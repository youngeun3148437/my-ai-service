"""질문 원본 확인 → AI 생성 → 답변 원본 저장 → 대화 캐시 정리 순서를 연결한다."""

from contextlib import aclosing
from asyncio import CancelledError

from app.core.errors import ServiceError
from app.schemas import AnswerCreated, AnswerInput
from app.services.attachments import AttachmentService
from app.services.observability import AIRequestLogger
from app.stores.cache import ConversationCache
from app.stores.database import Database
from app.stores.gemini import GeminiGateway
from app.stores.ai_limits import AILimits
from app.streaming import sse_event, stream_error_event


class AnswerService:
    def __init__(self, db: Database, cache: ConversationCache, gateway: GeminiGateway, context_limit: int,
                 attachments: AttachmentService | None = None, logger: AIRequestLogger | None = None,
                 limits: AILimits | None = None):
        self.db, self.cache, self.gateway, self.context_limit = db, cache, gateway, context_limit
        self.attachments = attachments
        self.logger = logger
        self.limits = limits

    async def acquire_budget(self):
        return await self.limits.acquire() if self.limits is not None else None

    def begin_attempt(self, conversation_id, body, mode, count):
        return self.logger.begin(conversation_id, body, self.gateway.settings.gemini_model, mode, count) if self.logger else None

    async def finish_attempt(self, attempt, status, http_status, answer_id=None, usage=None, error_code=None):
        return await self.logger.finish(attempt, status, http_status, answer_id, usage, error_code) if attempt else 'disabled'

    @staticmethod
    def log_metadata(attempt, log_status):
        return {'attempt_id': str(attempt.id) if attempt else None, 'log_status': log_status,
                'event_status': attempt.event_status if attempt else 'disabled'}

    async def prepare(self, conversation_id: int, body: AnswerInput) -> tuple[list[dict], bool]:
        # 질문 저장 응답의 ID로 DB 원본을 읽는다. 첫 연결은 use_history=False로 질문 한 건만 사용한다.
        limit = self.context_limit if body.use_history else 1
        rows = await self.db.answer_context(conversation_id, body.question_message_id, limit)
        # 캐시가 아니라 현재 사용자 RLS로 읽은 DB 원본을 AI에 전달한다.
        if not rows or rows[0]["id"] != body.question_message_id:
            raise ServiceError(404, "QUESTION_NOT_FOUND", "이 대화에서 질문을 찾을 수 없다")
        if rows[0]["role"] != "user":
            raise ServiceError(422, "QUESTION_REQUIRED", "user 역할의 질문을 선택한다")
        selected, truncated = self.history_context(rows, limit) if body.use_history else ([rows[0]], False)
        if body.attachment_ids:
            if self.attachments is None:
                raise ServiceError(503, 'ATTACHMENTS_UNAVAILABLE', '첨부 저장소를 설정한다')
            parts = await self.attachments.ai_parts(conversation_id, body.attachment_ids)
            # 이번 질문의 복사본에만 파일 part를 붙인다. DB 원본과 지난 턴 메시지는 바꾸지 않는다.
            selected[-1] = {**selected[-1], 'attachment_parts': parts}
        return selected, truncated

    @staticmethod
    def history_context(rows: list[dict], limit: int) -> tuple[list[dict], bool]:
        # DB가 질문 ID까지 최신순으로 읽은 rows와 문맥 건수 limit를 받는다.
        # 반환값은 과거→현재 순서의 문맥과 잘림 여부다. use_history=True일 때만 호출한다.
        # 질문까지의 최근 원본을 시간 순서로 고르고 잘린 문맥을 표시한다.
        selected = list(reversed(rows[:limit]))
        # 최근 N건에서 잘린 맨 앞 assistant는 제외해 질문으로 시작하는 문맥을 만든다.
        while selected and selected[0]['role'] == 'assistant':
            selected.pop(0)
        return selected, len(rows) > len(selected)

    async def create(self, conversation_id: int, body: AnswerInput) -> AnswerCreated:
        # 두 번째 POST의 입력이다. 질문을 다시 INSERT하지 않고 준비한 원본으로 답변만 생성·저장한다.
        selected, truncated = await self.prepare(conversation_id, body)
        # 스트리밍과 같은 준비 조건이다. 서버 설정 오류는 AI 시도 시작 전에 거절한다.
        if not self.gateway.settings.gemini_api_key:
            raise ServiceError(503, 'AI_NOT_CONFIGURED', 'FastAPI의 GEMINI_API_KEY를 설정한다')
        lease = await self.acquire_budget()
        attempt = None
        usage, answer_id = None, None
        try:
            attempt = self.begin_attempt(conversation_id, body, 'normal', len(selected))
            # generated를 받은 직후 usage를 갱신하고, DB가 row를 돌려준 직후 answer_id를 갱신한다.
            # result에 AnswerCreated 모델을 담는다. 아래 제공 코드가 종료 정보를 붙여 반환하므로 여기서 return하지 않는다.
            # AI 생성 후 assistant 원본을 저장하고 응답 모델을 만든다.
            generated = await self.gateway.generate(selected, body.answer_style)
            usage = generated.usage
            # 생성 실패면 INSERT하지 않는다. DB 응답 불명은 질문 자동 재전송으로 해결하지 않는다.
            row = await self.db.create_message(conversation_id, {"role": "assistant", "content": generated.text})
            answer_id = row['id']
            result = AnswerCreated(
                message=row, cache_cleared=await self.cache.clear(conversation_id),
                question_message_id=body.question_message_id, model=self.gateway.settings.gemini_model,
                context_message_count=len(selected), context_truncated=truncated,
                usage=usage, context_message_ids=[m['id'] for m in selected], attachment_ids=body.attachment_ids,
                attempt_id=attempt.id if attempt else None,
            )
            result.log_status = await self.finish_attempt(attempt, 'succeeded', 201, answer_id, usage)
            result.event_status = attempt.event_status if attempt else 'disabled'
            return result
        except ServiceError as error:
            state = 'succeeded' if answer_id is not None else 'failed'
            log_status = await self.finish_attempt(attempt, state, error.status_code, answer_id, usage, error.detail['code'])
            error.detail.update(self.log_metadata(attempt, log_status))
            raise
        except CancelledError:
            # 응답 전에 취소되면 전송된 HTTP 상태가 없다. 상태를 임의의 코드로 채우지 않는다.
            await self.finish_attempt(attempt, 'succeeded' if answer_id is not None else 'cancelled',
                                      None, answer_id, usage, 'CLIENT_DISCONNECTED' if answer_id is None else None)
            raise
        except Exception as error:
            log_status = await self.finish_attempt(attempt, 'succeeded' if answer_id is not None else 'failed',
                                                   500, answer_id, usage, 'AI_ANSWER_ERROR')
            failure = ServiceError(500, 'AI_ANSWER_ERROR', 'AI 답변 처리 결과를 확인한다')
            failure.detail.update(self.log_metadata(attempt, log_status))
            raise failure from error
        finally:
            if lease is not None:
                await lease.release()

    async def stream(self, conversation_id: int, body: AnswerInput, selected: list[dict], truncated: bool):
        # prepare가 검증한 질문 문맥을 받아 meta → delta → 저장된 답변의 done을 차례로 보낸다.
        # async + yield는 준비된 조각을 순서대로 넘긴다. 이 메서드를 호출만 하면 아직 실행되지 않는다.
        attempt = self.begin_attempt(conversation_id, body, 'stream', len(selected))
        usage, answer_id, terminal_written = None, None, False
        try:
            # meta 시점에는 로그가 저장되지 않았다. 종료 이벤트에만 log_status를 포함한다.
            yield sse_event('meta', {'question_message_id': body.question_message_id,
                'context_message_ids': [m['id'] for m in selected], 'context_truncated': truncated,
                'attachment_ids': [str(value) for value in body.attachment_ids],
                'attempt_id': str(attempt.id) if attempt else None})
            # aclosing은 브라우저 종료로 바깥 반복이 취소되어도 안쪽 제너레이터를 정리한다.
            async with aclosing(self.gateway.stream(selected, body.answer_style)) as events:
                async for event in events:
                    # 사건마다 result를 비운다. 정상 완성 answer가 있을 때만 사용량을 준비한다.
                    result = None
                    if event.answer is not None:
                        usage = event.answer.usage
                    # gateway의 delta는 표시용 조각이고 answer는 정상 완료를 검증한 전체 글이다.
                    # 완성 answer에서는 row를 저장하고 result에 AnswerCreated를 담는다. done·return은 아래 코드가 처리한다.
                    # delta 전달 또는 완성 답변의 저장·캐시 정리·result 모델을 연결한다.
                    if event.delta:
                        yield sse_event('delta', {'text': event.delta})
                    elif event.answer is not None:
                        row = await self.db.create_message(conversation_id, {'role': 'assistant', 'content': event.answer.text})
                        result = AnswerCreated(message=row, cache_cleared=await self.cache.clear(conversation_id),
                            question_message_id=body.question_message_id, model=self.gateway.settings.gemini_model,
                            context_message_count=len(selected), context_message_ids=[m['id'] for m in selected],
                            context_truncated=truncated, usage=usage,
                            attachment_ids=body.attachment_ids)
                    if result is not None:
                        # DB 응답을 포함한 모델에서 확정 ID를 읽고 종료 정보를 붙인다. 조각에서는 실행하지 않는다.
                        answer_id = result.message.id
                        result.attempt_id = attempt.id if attempt else None
                        result.log_status = await self.finish_attempt(attempt, 'succeeded', 200, answer_id, usage)
                        result.event_status = attempt.event_status if attempt else 'disabled'
                        terminal_written = True
                        # done은 DB 저장 응답까지 받은 결과다. 단순 모델 종료 표시가 아니다.
                        yield sse_event('done', result.model_dump(mode='json'))
                        return
            raise ServiceError(502, 'AI_STREAM_INCOMPLETE', '완료 결과를 받지 못했다')
        except ServiceError as error:
            # HTTP 200은 이미 시작했다. 이후 오류는 사건으로 전달하고 자동 재생성하지 않는다.
            # 제공된 오류 사건 변환으로 실패를 전달한다. 정상 meta/delta/done에서는 이 분기를 거치지 않는다.
            log_status = await self.finish_attempt(attempt, 'succeeded' if answer_id is not None else 'failed',
                                                   200, answer_id, usage, error.detail['code'])
            terminal_written = True
            yield stream_error_event(error, self.log_metadata(attempt, log_status))
        except Exception:
            log_status = await self.finish_attempt(attempt, 'succeeded' if answer_id is not None else 'failed',
                                                   200, answer_id, usage, 'AI_STREAM_ERROR')
            terminal_written = True
            yield sse_event('error', {'status': 500, 'code': 'AI_STREAM_ERROR', 'message': '답변 저장 결과를 확인한다',
                                     **self.log_metadata(attempt, log_status)})
        finally:
            if not terminal_written:
                # GeneratorExit·취소도 이 finally를 실행한다. 부분 글은 저장하지 않는다.
                await self.finish_attempt(attempt, 'succeeded' if answer_id is not None else 'cancelled',
                                          200, answer_id, usage, 'CLIENT_DISCONNECTED' if answer_id is None else None)
