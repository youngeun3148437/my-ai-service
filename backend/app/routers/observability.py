"""평가 요청과 기간·종료 상태·생성 방식으로 제한한 로그·대시보드 요청을 연결한다."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import AwareDatetime, ValidationError

from app.core.dependencies import Authenticated, SharedResources
from app.core.errors import ServiceError
from app.schemas import (AIRequestLogPage, Dashboard, FeedbackCreated, FeedbackInput,
                         FeedbackPage, LogFilters, PositiveId)
from app.services.observability import ObservabilityService
from app.stores.database import Database

router = APIRouter(tags=['평가·요청 로그·대시보드'])


def get_observability_service(context: Authenticated, resources: SharedResources) -> ObservabilityService:
    if not resources.settings.observability_enabled:
        raise ServiceError(503, 'OBSERVABILITY_UNAVAILABLE',
            '피드백·로그 SQL 적용 후 OBSERVABILITY_ENABLED=true로 설정하고 서버를 재시작한다')
    return ObservabilityService(Database(context))


Observability = Annotated[ObservabilityService, Depends(get_observability_service)]


def get_filters(since: Annotated[AwareDatetime | None, Query()] = None,
                until: Annotated[AwareDatetime | None, Query()] = None,
                status: Literal['succeeded', 'failed', 'cancelled'] | None = None,
                mode: Literal['normal', 'stream'] | None = None) -> LogFilters:
    try:
        return LogFilters(since=since, until=until, status=status, mode=mode)
    except ValidationError as error:
        raise ServiceError(422, 'LOG_PERIOD_INVALID', '기간 끝은 시작보다 뒤이며 범위는 31일 이내여야 한다') from error


Filters = Annotated[LogFilters, Depends(get_filters)]


@router.post('/answers/{answer_id}/feedback', response_model=FeedbackCreated, status_code=201,
             responses={200: {'model': FeedbackCreated}})
async def create_feedback(answer_id: PositiveId, body: FeedbackInput, response: Response, service: Observability):
    result = await service.feedback(answer_id, body)
    response.status_code = 200 if result.replayed else 201
    return result


@router.get('/answers/{answer_id}/feedback', response_model=FeedbackPage)
async def feedbacks(answer_id: PositiveId, service: Observability,
                    before_id: Annotated[int | None, Query(gt=0)] = None,
                    limit: Annotated[int, Query(ge=1, le=100)] = 20):
    return await service.feedbacks(answer_id, before_id, limit)


@router.get('/ai-request-logs', response_model=AIRequestLogPage)
async def logs(service: Observability, filters: Filters,
               before_id: Annotated[int | None, Query(gt=0)] = None,
               limit: Annotated[int, Query(ge=1, le=100)] = 20):
    return await service.logs(filters, before_id, limit)


@router.get('/dashboard', response_model=Dashboard)
async def dashboard(service: Observability, filters: Filters):
    return await service.dashboard(filters)
