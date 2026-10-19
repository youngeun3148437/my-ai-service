"""서버 시작·종료에 맞춰 공유 HTTP·Redis 연결 풀을 준비하고 정리한다."""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from asyncio import Event

import httpx
from anyio import move_on_after
from fastapi import FastAPI
from redis.asyncio import Redis

from app.core.settings import Settings, get_settings


# dataclass는 설정·HTTP·Redis를 함께 전달하는 객체의 초기화를 만든다.
@dataclass
class Resources:
    settings: Settings
    http: httpx.AsyncClient
    redis: Redis
    # SSE는 요청마다 전용 PubSub 연결을 빌린다. 서버 종료 시 이 연결들도 먼저 정리한다.
    shutdown: Event = field(default_factory=Event)
    event_subscriptions: set = field(default_factory=set)


# asynccontextmanager는 yield 전의 준비와 뒤의 정리를 하나의 수명으로 묶는다.
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings_override or get_settings()
    # 비어 있거나 잘못된 세션 암호화 키는 로그인 요청을 받기 전에 확인한다.
    from app.services.login_sessions import get_login_session_settings
    get_login_session_settings()
    # 연결 풀은 서버 프로세스마다 하나다. 여러 요청이 같은 연결들을 재사용한다.
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_connections,
        ),
    ) as http:
        redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=settings.redis_timeout_seconds,
            socket_connect_timeout=settings.redis_timeout_seconds,
            max_connections=settings.redis_max_connections,
        )
        app.state.resources = Resources(settings, http, redis)
        try:
            # yield 전은 서버 준비, 뒤는 종료다. 요청별 정리와 구분한다.
            yield
        finally:
            app.state.resources.shutdown.set()
            for subscription in tuple(app.state.resources.event_subscriptions):
                with move_on_after(2, shield=True):
                    await subscription.aclose()
            app.state.resources.event_subscriptions.clear()
            await redis.aclose()
            app.state.resources = None
    # async with를 나가면서 HTTP 연결 풀도 닫힌다.
