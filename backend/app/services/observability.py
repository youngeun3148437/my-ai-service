"""답변 평가와 RLS로 제한한 요청 목록·전체 집계, 답변과 독립적인 종료 로그를 처리한다."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from uuid import UUID, uuid4

from anyio import move_on_after
from pydantic import ValidationError

from app.core.errors import ServiceError
from app.schemas import (AIRequestLogPage, Dashboard, FeedbackCreated, FeedbackInput,
                         FeedbackPage, LogFilters, TokenUsage)
from app.stores.database import Database


class ObservabilityService:
    def __init__(self, db: Database):
        self.db = db

    async def check_answer(self, answer_id: int):
        row = await self.db.answer(answer_id)
        if row['role'] != 'assistant':
            raise ServiceError(422, 'ANSWER_REQUIRED', 'assistant 답변에 평가를 남긴다')

    async def feedback(self, answer_id: int, body: FeedbackInput) -> FeedbackCreated:
        await self.check_answer(answer_id)
        if body.client_request_id is None:
            row, replayed = await self.db.create_feedback(answer_id, body.rating, body.comment), False
        else:
            row, replayed = await self.db.create_feedback_once(answer_id, body)
        return FeedbackCreated(feedback=row, replayed=replayed)

    async def feedbacks(self, answer_id: int, before_id: int | None, limit: int) -> FeedbackPage:
        # 본인 답변의 평가 목록과 이전 페이지 커서를 반환한다.
        await self.check_answer(answer_id)
        rows = await self.db.feedbacks(answer_id, before_id, limit)
        selected = rows[:limit]
        return FeedbackPage(feedbacks=selected,
            next_before_id=selected[-1]['id'] if len(rows) > limit else None)

    async def logs(self, filters: LogFilters, before_id: int | None, limit: int) -> AIRequestLogPage:
        rows = await self.db.ai_logs(filters, before_id, limit)
        selected = rows[:limit]
        return AIRequestLogPage(logs=selected,
            next_before_id=selected[-1]['id'] if len(rows) > limit else None)

    async def dashboard(self, filters: LogFilters) -> Dashboard:
        # 전체 조건 집계 RPC 결과를 응답 모델로 검사한다.
        try:
            return Dashboard.model_validate(await self.db.dashboard(filters))
        except ValidationError as error:
            raise ServiceError(502, 'DASHBOARD_UNAVAILABLE', '대시보드 집계 응답을 확인할 수 없다') from error


@dataclass
class AIAttempt:
    id: UUID
    values: dict
    started: float = field(default_factory=perf_counter)
    finished: bool = False
    log_status: str = 'unavailable'
    event_status: str = 'disabled'


class AIRequestLogger:
    def __init__(self, db: Database, timeout: float, request_id: str | None, publisher=None):
        self.db, self.timeout, self.request_id = db, timeout, request_id
        self.publisher = publisher

    def begin(self, conversation_id: int, body, model: str, mode: str, context_count: int) -> AIAttempt:
        # 시도 UUID·시작 시각·질문 관계를 준비한다.
        attempt_id = uuid4()
        return AIAttempt(attempt_id, {
            'attempt_id': str(attempt_id), 'http_request_id': self.request_id,
            'auth_user_id': str(self.db.context.auth_user_id), 'conversation_id': conversation_id,
            'question_message_id': body.question_message_id, 'model': model, 'mode': mode,
            'started_at': datetime.now(timezone.utc).isoformat(),
            'context_message_count': context_count, 'attachment_count': len(body.attachment_ids),
        }, event_status='skipped' if self.publisher is not None else 'disabled')

    async def finish(self, attempt: AIAttempt, status: str, http_status: int | None,
                     answer_id: int | None, usage: TokenUsage | None, error_code: str | None) -> str:
        if attempt.finished:
            return attempt.log_status
        attempt.finished = True
        # duration은 종료를 판단한 순간까지다. 아래 로그 INSERT 대기 시간은 더하지 않는다.
        values = {**attempt.values, 'status': status, 'http_status': http_status,
                  'answer_message_id': answer_id, 'duration_ms': max(0, int((perf_counter() - attempt.started) * 1000)),
                  'error_code': error_code, **(usage or TokenUsage()).model_dump()}
        try:
            # shield는 바깥 요청이 취소되어도 짧은 정리 시간을 허용한다. timeout 뒤에는 계속 기다리지 않는다.
            with move_on_after(self.timeout, shield=True) as scope:
                # 종료 값의 영구 저장 결과로 로그 ID를 받는다.
                log_id = await self.db.create_ai_log(values)
            if not scope.cancel_called:
                attempt.log_status = 'saved'
                if self.publisher is not None:
                    attempt.event_status = 'unavailable'
                    # DB가 PK를 확인해 준 뒤에만 발행한다. 발행 실패는 저장 성공을 되돌리지 않는다.
                    attempt.event_status = 'published' if await self.publisher.publish(log_id, attempt.id) else 'unavailable'
        except Exception:
            # 저장 실패의 원문·토큰을 로그에 다시 남기지 않는다. 답변 저장 결과도 바꾸지 않는다.
            pass
        return attempt.log_status
