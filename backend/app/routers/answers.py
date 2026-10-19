"""저장된 질문의 ID를 받아 AI 답변을 생성·저장하는 보호 API다."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.dependencies import Authenticated, SharedResources
from app.core.errors import ServiceError
from app.schemas import AnswerCreated, AnswerInput, PositiveId
from app.services.answers import AnswerService
from app.services.attachments import AttachmentService
from app.services.observability import AIRequestLogger
from app.stores.cache import ConversationCache
from app.stores.database import Database
from app.stores.gemini import GeminiGateway
from app.stores.storage import StorageGateway
from app.stores.log_events import LogEventPublisher
from app.stores.ai_limits import AILimits
from app.streaming import ManagedStreamingResponse, TimedEventResponse

router = APIRouter(tags=["AI 답변"])


def get_answer_service(context: Authenticated, resources: SharedResources, request: Request = None) -> AnswerService:
    settings = resources.settings
    db = Database(context, attachments_enabled=settings.attachments_enabled)
    return AnswerService(
        db,
        ConversationCache(resources.redis, str(context.auth_user_id), settings.cache_ttl_seconds, settings.message_page_size),
        GeminiGateway(resources.http, settings), settings.ai_context_limit,
        AttachmentService(db, StorageGateway(resources.http, settings, context.access_token))
        if settings.attachments_enabled else None,
        AIRequestLogger(db, settings.log_write_timeout_seconds,
                        getattr(request.state, 'request_id', None) if request else None,
                        LogEventPublisher(resources.redis, context.auth_user_id, settings.redis_timeout_seconds)
                        if settings.realtime_enabled else None)
        if settings.observability_enabled else None,
        AILimits(resources.redis, str(context.auth_user_id), settings),
    )


Answers = Annotated[AnswerService, Depends(get_answer_service)]


@router.post("/conversations/{conversation_id}/answers", response_model=AnswerCreated, status_code=201)
async def create_answer(conversation_id: PositiveId, body: AnswerInput, service: Answers):
    # 요청별 신원·저장소를 주입받는다. 생성 도중 자동 재시도는 하지 않는다.
    return await service.create(conversation_id, body)


@router.post('/conversations/{conversation_id}/answers/stream', response_class=ManagedStreamingResponse,
             responses={200: {'content': {'text/event-stream': {'schema': {'type': 'string'}}}}})
async def stream_answer(conversation_id: PositiveId, body: AnswerInput, service: Answers):
    # body의 저장된 질문 ID로 답변만 생성한다. 질문을 다시 저장하는 요청이 아니다.
    # 200을 시작하기 전에 질문의 소유권·역할·설정을 검증한다. 일반 오류는 JSON HTTP 응답이다.
    selected, truncated = await service.prepare(conversation_id, body)
    if not service.gateway.settings.gemini_api_key:
        raise ServiceError(503, 'AI_NOT_CONFIGURED', 'FastAPI의 GEMINI_API_KEY를 설정한다')
    # 헤더를 보내기 전에 접수한다. 제한 오류는 JSON HTTP 오류이며 질문을 다시 저장하지 않는다.
    lease = await service.acquire_budget()
    try:
        return TimedEventResponse(service.stream(conversation_id, body, selected, truncated),
            close_callback=lease.release if lease is not None else None,
            media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    except BaseException:
        if lease is not None:
            await lease.release()
        raise
