"""현재 사용자 JWT로 보호된 로그 알림 SSE를 연다. 클라이언트는 채널을 지정하지 않는다."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.dependencies import Authenticated, SharedResources
from app.services.realtime import RealtimeService
from app.streaming import TimedEventResponse

router = APIRouter(tags=['실시간 요청 로그 알림'])


def get_realtime_service(context: Authenticated, resources: SharedResources) -> RealtimeService:
    return RealtimeService(context, resources)


Realtime = Annotated[RealtimeService, Depends(get_realtime_service)]


@router.get('/ai-request-events', response_class=TimedEventResponse,
            responses={200: {'content': {'text/event-stream': {'schema': {'type': 'string'}}}}})
async def ai_request_events(service: Realtime):
    # 인증·기능 설정·Redis 구독 확인을 HTTP 200 시작 전에 마친다.
    subscription, expires = await service.prepare()
    return TimedEventResponse(service.events(subscription, expires),
        send_timeout=service.resources.settings.realtime_send_timeout_seconds,
        close_callback=subscription.close,
        media_type='text/event-stream', headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
