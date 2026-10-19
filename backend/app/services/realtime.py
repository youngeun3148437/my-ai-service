"""인증된 사용자 채널의 알림을 SSE로 넘기고 수명·만료·재연결을 제한한다."""

import base64
import json
from datetime import datetime, timezone
from time import monotonic, time
from math import isfinite

from anyio import fail_after
from pydantic import ValidationError
from redis.exceptions import RedisError

from app.core.errors import ServiceError
from app.schemas import LogSavedEvent
from app.stores.log_events import LogEventSubscription
from app.streaming import sse_event


def verified_token_expiry(context) -> float:
    # get_user_context가 이미 Supabase get_user로 검증한 JWT의 exp만 읽는다.
    # 이 디코딩은 서명 검증을 대신하지 않는다. 원본 토큰을 URL·이벤트에 넣지 않는다.
    try:
        encoded = context.access_token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        expires = claims['exp']
        if not isinstance(expires, (int, float)) or isinstance(expires, bool) or not isfinite(expires):
            raise ValueError('expiry required')
        return float(expires)
    except (ValueError, TypeError, IndexError, KeyError, AttributeError) as error:
        raise ServiceError(401, 'TOKEN_EXPIRY_REQUIRED', 'Access token의 만료 시각을 확인한다') from error


class RealtimeService:
    def __init__(self, context, resources):
        self.context, self.resources = context, resources

    async def prepare(self):
        settings = self.resources.settings
        if not settings.realtime_enabled or not settings.observability_enabled:
            raise ServiceError(503, 'REALTIME_UNAVAILABLE', '로그 SQL·OBSERVABILITY_ENABLED=true 이후 REALTIME_ENABLED=true로 설정한다')
        if self.resources.shutdown.is_set():
            raise ServiceError(503, 'REALTIME_UNAVAILABLE', '서버 연결을 종료하는 중이다')
        expires = verified_token_expiry(self.context)
        if expires <= time():
            raise ServiceError(401, 'TOKEN_EXPIRED', '새 Access token으로 구독한다')
        subscription = LogEventSubscription(self.resources, self.context.auth_user_id)
        await subscription.connect()
        # ACK를 기다리는 사이 서버 종료가 시작될 수 있다. 준비된 연결을 다시 확인한다.
        if self.resources.shutdown.is_set():
            await subscription.close()
            raise ServiceError(503, 'REALTIME_UNAVAILABLE', '서버 연결을 종료하는 중이다')
        if expires <= time():
            await subscription.close()
            raise ServiceError(401, 'TOKEN_EXPIRED', '새 Access token으로 구독한다')
        return subscription, expires

    async def events(self, subscription, expires):
        settings = self.resources.settings
        # 준비 후 헤더 전달이 지연된 시간도 연결 수명에서 뺀다.
        remaining = max(0, min(settings.realtime_connection_seconds - (monotonic() - subscription.subscribed_at), expires - time()))
        # JWT exp는 실제 UTC 시각으로 검사한다. 별도 시계로 환산하면 경계에서 종료 이유가 달라질 수 있다.
        deadline = subscription.subscribed_at + settings.realtime_connection_seconds
        next_heartbeat = monotonic() + settings.realtime_heartbeat_seconds
        try:
            yield sse_event('ready', {'channel': 'user', 'connection_lifetime_seconds': remaining, 'replay': False})
            while True:
                if time() >= expires:
                    yield sse_event('error', {'code': 'AUTH_EXPIRED', 'message': '새 Access token으로 다시 연결한다'})
                    return
                if monotonic() >= deadline:
                    yield sse_event('error', {'code': 'REALTIME_RECONNECT', 'message': '연결 수명이 끝났다. 다시 연결하고 DB 목록을 조회한다'})
                    return
                if self.resources.shutdown.is_set():
                    yield sse_event('error', {'code': 'REALTIME_UNAVAILABLE', 'message': '서버 연결을 종료한다. 다시 연결한 뒤 DB 목록을 조회한다'})
                    return
                wait = max(0.001, min(1, deadline - monotonic(), expires - time(), next_heartbeat - monotonic(), settings.redis_timeout_seconds / 2))
                with fail_after(settings.redis_timeout_seconds):
                    message = await subscription.pubsub.get_message(ignore_subscribe_messages=False, timeout=wait)
                if message and message['type'] == 'subscribe':
                    # 라이브러리가 다시 구독했다면 중간 사건이 빠질 수 있다. 조용히 이어 읽지 않는다.
                    yield sse_event('error', {'code': 'REALTIME_RECONNECT', 'message': '구독 연결이 바뀌었다. DB 목록을 다시 조회한다'})
                    return
                if message and message['type'] == 'message' and message['channel'] == subscription.channel:
                    data = message.get('data', '')
                    if isinstance(data, (str, bytes)) and len(data) <= 1024:
                        try:
                            event = LogSavedEvent.model_validate_json(data)
                            yield sse_event('log.saved', event.model_dump(mode='json'))
                        except ValidationError:
                            pass
                if monotonic() >= next_heartbeat:
                    # 업무 사건이 없어도 연결이 살아 있음을 보여 준다. 원본 복구 데이터는 아니다.
                    heartbeat = heartbeat_event()
                    if heartbeat is not None:
                        yield heartbeat
                    next_heartbeat = monotonic() + settings.realtime_heartbeat_seconds
        except (RedisError, TimeoutError, RuntimeError):
            yield sse_event('error', {'code': 'REALTIME_UNAVAILABLE', 'message': '구독 연결을 확인할 수 없다. DB 목록을 조회한다'})
        finally:
            await subscription.close()


def heartbeat_event() -> str | None:
    # 업무 사건이 없는 동안 heartbeat를 보내 연결 유지와 원본 사건 전달을 구분한다.
    return sse_event('heartbeat', {'time': datetime.now(timezone.utc).isoformat()})
