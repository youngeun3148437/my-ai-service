"""로그 저장 알림을 사용자별 Redis 채널에 발행하고 구독 확인·연결 정리를 관리한다."""

from uuid import UUID, uuid4
from time import monotonic

from anyio import fail_after, move_on_after
from redis.exceptions import RedisError

from app.core.errors import ServiceError
from app.schemas import LogSavedEvent


def user_channel(user_id: UUID) -> str:
    # 외부 입력으로 채널 이름을 받지 않는다. Auth에서 확인한 사용자 UUID만 사용한다.
    return f'course:ai-logs:{user_id}'


class LogEventPublisher:
    def __init__(self, redis, user_id: UUID, timeout: float):
        self.redis, self.channel, self.timeout = redis, user_channel(user_id), timeout

    async def publish(self, log_id: int, attempt_id: UUID) -> bool:
        try:
            # 본인 채널에 최소 로그 사건 JSON을 발행한다.
            event = LogSavedEvent(event_id=uuid4(), attempt_id=attempt_id, log_id=log_id)
            with move_on_after(self.timeout, shield=True) as scope:
                await self.redis.publish(self.channel, event.model_dump_json())
            # 반환된 구독자 수가 0이어도 발행은 성공한다. 처리 완료의 확인은 아니다.
            return not scope.cancel_called
        except Exception:
            return False


class LogEventSubscription:
    def __init__(self, resources, user_id: UUID):
        self.resources, self.channel = resources, user_channel(user_id)
        self.pubsub = resources.redis.pubsub(ignore_subscribe_messages=False)
        self.closed = False
        self.subscribed_at = monotonic()

    async def connect(self):
        self.resources.event_subscriptions.add(self.pubsub)
        ready = False
        try:
            # subscribe()는 명령을 보낸다. Redis가 보낸 subscribe 응답을 읽어야 준비 완료다.
            with fail_after(self.resources.settings.redis_timeout_seconds):
                # SUBSCRIBE 후 같은 채널의 준비 ACK까지 확인한다.
                await self.pubsub.subscribe(self.channel)
                while True:
                    message = await self.pubsub.get_message(ignore_subscribe_messages=False, timeout=0.1)
                    if message and message['type'] == 'subscribe' and message['channel'] == self.channel:
                        ready = True
                        self.subscribed_at = monotonic()
                        return
        except (RedisError, TimeoutError) as error:
            raise ServiceError(503, 'REALTIME_UNAVAILABLE', '실시간 구독을 준비할 수 없다. DB 목록을 조회한다') from error
        finally:
            if not ready:
                # 준비 도중 취소되어도 이미 빌린 전용 연결은 반환한다.
                await self.close()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            with move_on_after(0.5, shield=True):
                try:
                    connection = getattr(self.pubsub, 'connection', None)
                    # 이미 닫힌 연결에 UNSUBSCRIBE하면 클라이언트가 다시 접속할 수 있다.
                    if not self.resources.shutdown.is_set() and connection is not None and connection.is_connected:
                        await self.pubsub.unsubscribe(self.channel)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            with move_on_after(2, shield=True):
                try:
                    # 구독 연결만 반환한다. 공유 Redis 클라이언트·HTTP 풀은 닫지 않는다.
                    await self.pubsub.aclose()
                except Exception:
                    pass
            self.resources.event_subscriptions.discard(self.pubsub)
