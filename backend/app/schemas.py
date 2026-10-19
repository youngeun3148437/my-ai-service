"""API 입력을 검증하고 프로필·대화·메시지·초안 응답의 모양을 정한다."""

from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Path
from pydantic import AwareDatetime, BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

# Annotated는 int에 FastAPI 검증 조건을 붙인다. 경로의 ID는 0보다 커야 한다.
PositiveId = Annotated[int, Path(gt=0)]


class Input(BaseModel):
    # 예상하지 않은 입력은 거절하고, 문자열 양끝의 공백을 제거한다.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProfileInput(Input):
    display_name: str = Field(min_length=1, max_length=40)


class ProfileRecord(BaseModel):
    id: int
    auth_user_id: UUID
    display_name: str


class ConversationInput(Input):
    title: str = Field(min_length=1, max_length=100)


class ConversationRecord(BaseModel):
    id: int
    user_id: int
    title: str
    created_at: datetime


class ConversationPage(BaseModel):
    conversations: list[ConversationRecord]
    next_before_id: int | None


class MessageInput(Input):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=5000)
    # 선택적인 초안 ID는 저장 후 정리할 대상이다. 메시지 본문은 항상 content를 쓴다.
    session_id: UUID | None = None
    # 마지막으로 확인한 초안 버전이다. 새로 수정된 초안은 전송 후에도 지우지 않는다.
    session_revision: UUID | None = None
    # 같은 논리적 전송을 재시도할 때 유지한다. 초안 ID·진단용 HTTP 요청 번호와 별개다.
    client_request_id: UUID | None = None

    # after는 개별 필드 검증 뒤 모델 전체의 관계를 검사한다. 검증한 self를 반환한다.
    @model_validator(mode="after")
    def draft_reference(self):
        if self.session_revision is not None and self.session_id is None:
            raise ValueError("초안 버전에는 session_id가 필요하다")
        return self


class MessageUpdate(Input):
    content: str = Field(min_length=1, max_length=5000)


class MessageRecord(BaseModel):
    id: int
    conversation_id: int
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class MessagePage(BaseModel):
    messages: list[MessageRecord]
    next_before_id: int | None
    source: Literal["cache", "supabase", "supabase_fallback"]


class MessageWrite(BaseModel):
    message: MessageRecord
    cache_cleared: bool


class Cleanup(BaseModel):
    deleted: bool = True
    cache_cleared: bool
    drafts_cleared: bool | None = None


class DraftInput(Input):
    draft_question: str = Field(min_length=1, max_length=5000)


class DraftRecord(BaseModel):
    session_id: UUID
    conversation_id: int
    draft_question: str
    ttl_seconds: int
    revision: UUID


class DraftUpdate(DraftInput):
    revision: UUID


class ActiveDraft(BaseModel):
    draft: DraftRecord | None


class MessageCreated(MessageWrite):
    # session_id가 없으면 None이다. 원본 저장 성공과 초안 정리 결과를 구분한다.
    draft_cleanup: Literal["cleared", "not_found", "changed", "unavailable"] | None = None
    replayed: bool = False


class AnswerInput(Input):
    # 두 번째 POST에서 받을 요청 형식이다. 이미 저장한 질문의 ID와 답변 조건을 받는다.
    # 저장된 질문을 가리킨다. UI 입력을 다시 INSERT하거나 Redis 초안을 읽지 않는다.
    question_message_id: int = Field(gt=0)
    answer_style: Literal["brief", "example"] = "brief"
    use_history: bool = False
    # 이번 질문에 함께 보낼 준비 완료 파일이다. 이전 질문의 첨부는 자동으로 다시 보내지 않는다.
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=2)

    @field_validator('attachment_ids')
    @classmethod
    def distinct_attachments(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('첨부 ID는 중복 없이 선택한다')
        return values


class TokenUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class AnswerCreated(MessageWrite):
    question_message_id: int
    model: str
    context_message_count: int
    context_truncated: bool
    usage: TokenUsage
    context_message_ids: list[int] = Field(default_factory=list)
    attachment_ids: list[UUID] = Field(default_factory=list)
    attempt_id: UUID | None = None
    log_status: Literal['disabled', 'saved', 'unavailable'] = 'disabled'
    event_status: Literal['disabled', 'published', 'unavailable', 'skipped'] = 'disabled'


class LogSavedEvent(BaseModel):
    # Redis에서 읽은 데이터도 모양을 검사한다. 본문·토큰 같은 추가 필드는 전달하지 않는다.
    model_config = ConfigDict(extra='forbid')
    event_id: UUID
    attempt_id: UUID
    log_id: int = Field(gt=0, strict=True)


class AttachmentRecord(BaseModel):
    id: UUID
    conversation_id: int
    auth_user_id: UUID
    filename: str
    content_type: Literal['image/png', 'image/jpeg', 'application/pdf']
    byte_size: int = Field(gt=0, le=5 * 1024 * 1024)
    status: Literal['pending', 'ready']
    object_path: str
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    created_at: datetime


class AttachmentPage(BaseModel):
    attachments: list[AttachmentRecord]


class AttachmentDeleted(BaseModel):
    deleted: bool = True


class FeedbackInput(Input):
    rating: int = Field(ge=1, le=5)
    comment: str = Field(min_length=1, max_length=1000)
    client_request_id: UUID | None = None


class FeedbackRecord(BaseModel):
    id: int
    answer_message_id: int
    rating: int = Field(ge=1, le=5)
    comment: str
    client_request_id: UUID | None = None
    created_at: datetime


class FeedbackCreated(BaseModel):
    feedback: FeedbackRecord
    replayed: bool = False


class FeedbackPage(BaseModel):
    feedbacks: list[FeedbackRecord]
    next_before_id: int | None


class LogFilters(BaseModel):
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    status: Literal['succeeded', 'failed', 'cancelled'] | None = None
    mode: Literal['normal', 'stream'] | None = None

    @model_validator(mode='after')
    def period(self):
        if self.since is not None and self.until is not None:
            if self.until <= self.since or self.until - self.since > timedelta(days=31):
                raise ValueError('기간 끝은 시작보다 뒤이며 범위는 31일 이내여야 한다')
        return self


class AIRequestLog(BaseModel):
    id: int
    attempt_id: UUID
    http_request_id: UUID | None
    auth_user_id: UUID
    conversation_id: int | None
    question_message_id: int | None
    answer_message_id: int | None
    model: str
    status: Literal['succeeded', 'failed', 'cancelled']
    mode: Literal['normal', 'stream']
    http_status: int | None
    started_at: datetime
    duration_ms: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    context_message_count: int
    attachment_count: int
    error_code: str | None
    created_at: datetime


class AIRequestLogPage(BaseModel):
    logs: list[AIRequestLog]
    next_before_id: int | None


class DashboardSummary(BaseModel):
    total_requests: int
    succeeded: int
    failed: int
    cancelled: int
    success_rate: float
    average_duration_ms: float | None
    known_total_tokens: int
    usage_known_requests: int


class DailySummary(BaseModel):
    date: str
    total_requests: int
    succeeded: int
    failed: int
    cancelled: int
    known_total_tokens: int
    usage_known_requests: int


class RatingCount(BaseModel):
    rating: int
    count: int


class FeedbackSummary(BaseModel):
    count: int
    average_rating: float | None
    ratings: list[RatingCount]


class Dashboard(BaseModel):
    summary: DashboardSummary
    daily: list[DailySummary]
    feedback: FeedbackSummary
    filters: LogFilters


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class SignupInput(LoginInput):
    @field_validator("password")
    @classmethod
    def password_policy(cls, value: str) -> str:
        groups = ("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "0123456789",
                  "!@#$%^&*()_+-=[]{};'\":\\|<>?,./`~")
        if len(value) < 12 or len(value.encode("utf-8")) > 72 or not all(
            any(character in group for character in value) for group in groups
        ):
            raise ValueError("12자 이상이며 영문 대문자·소문자·숫자·기호를 포함하고 72바이트 이하여야 한다")
        return value


class SignupResponse(BaseModel):
    user_id: UUID
    email_confirmation_required: bool


class Tokens(BaseModel):
    user_id: UUID
    token_type: Literal["bearer"] = "bearer"
    access_token: str
    refresh_token: str
    expires_in: int


class RefreshInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(min_length=1)
