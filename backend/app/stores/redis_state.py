"""대화별 공통 초안 하나를 저장하고 버전 비교·변경을 Redis 안에서 처리한다."""

from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError
from redis import RedisError
from redis.asyncio import Redis

from app.core.errors import ServiceError
from app.schemas import DraftRecord

# Lua 실행 중에는 다른 Redis 명령이 끼어들지 않는다. 조회와 변경 사이의 틈을 막는다.
# KEYS는 키, ARGV는 전달한 값이다. 0=없음, -1=다른 버전, 1=성공이다.
UPDATE_IF_CURRENT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local state = cjson.decode(raw)
if state.session_id ~= ARGV[1] then return 0 end
if state.revision ~= ARGV[2] then return -1 end
redis.call('SET', KEYS[1], ARGV[3], 'EX', ARGV[4], 'XX')
return 1
"""
DELETE_IF_CURRENT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local state = cjson.decode(raw)
if state.session_id ~= ARGV[1] then return 0 end
if state.revision ~= ARGV[2] then return -1 end
return redis.call('DEL', KEYS[1])
"""


class StoredDraft(BaseModel):
    session_id: UUID
    conversation_id: int
    draft_question: str
    revision: UUID


class WorkSessions:
    def __init__(self, redis: Redis, user_id: str, ttl: int):
        self.redis, self.user_id, self.ttl = redis, user_id, ttl
        # EVALSHA로 재사용한다. 서버에 없으면 redis-py가 스크립트를 다시 등록한다.
        self.update_script = redis.register_script(UPDATE_IF_CURRENT)
        self.delete_script = redis.register_script(DELETE_IF_CURRENT)

    def key(self, conversation_id: int) -> str:
        # 한 사용자·대화에 하나다. DB 기초 예제의 여러 세션 키와 별도 이름을 사용한다.
        return f"course:ui-draft:{self.user_id}:{conversation_id}"

    async def create(self, conversation_id: int, question: str) -> DraftRecord:
        state = StoredDraft(session_id=uuid4(), conversation_id=conversation_id,
                            draft_question=question, revision=uuid4())
        try:
            created = await self.redis.set(self.key(conversation_id), state.model_dump_json(),
                                           ex=self.ttl, nx=True)
        except RedisError as error:
            raise ServiceError(503, "DRAFT_UNAVAILABLE", "초안을 저장할 수 없다") from error
        if not created:
            raise ServiceError(409, "DRAFT_EXISTS", "다른 화면이 저장한 초안을 먼저 확인한다")
        return DraftRecord(**state.model_dump(), ttl_seconds=self.ttl)

    async def active(self, conversation_id: int) -> DraftRecord | None:
        try:
            # 현재 사용자의 대화 키에서 본문과 남은 수명을 읽는다. 조회로 수명을 늘리지 않는다.
            key = self.key(conversation_id)
            # 활성 초안 본문과 남은 TTL을 읽는다.
            raw = await self.redis.get(key)
            ttl = await self.redis.ttl(key) if raw is not None else -2
            # 키가 없거나 유효한 TTL이 없으면 collection 응답의 draft는 None이다.
            if raw is None or ttl < 0:
                return None
            # JSON을 검증한 뒤 읽은 ID·revision·남은 TTL을 응답에 함께 담는다.
            state = StoredDraft.model_validate_json(raw)
            if state.conversation_id != conversation_id:
                raise ValueError("초안의 대화 번호 불일치")
            # ttl=0은 만료 직전이다. 조회는 TTL을 연장하지 않는다.
            return DraftRecord(**state.model_dump(), ttl_seconds=ttl)
        except (RedisError, ValueError, ValidationError) as error:
            raise ServiceError(503, "DRAFT_UNAVAILABLE", "초안을 읽을 수 없다") from error

    async def read(self, conversation_id: int, session_id: UUID) -> DraftRecord:
        state = await self.active(conversation_id)
        if state is None or state.session_id != session_id:
            raise ServiceError(404, "DRAFT_NOT_FOUND", "작업 세션이 없거나 만료됐다")
        return state

    async def update(self, conversation_id: int, session_id: UUID, question: str,
                     revision: UUID) -> DraftRecord:
        # 읽은 초안 ID는 유지하고 성공한 수정본에 사용할 revision은 새 UUID로 만든다.
        state = StoredDraft(session_id=session_id, conversation_id=conversation_id,
                            draft_question=question, revision=uuid4())
        try:
            # Lua에 읽은 ID·revision과 새 JSON·TTL을 보낸다. 비교와 변경은 Redis 안에서 함께 실행한다.
            # 읽은 revision으로 원자적 수정을 요청한다.
            result = await self.update_script(keys=[self.key(conversation_id)],
                args=[str(session_id), str(revision), state.model_dump_json(), self.ttl])
        except RedisError as error:
            raise ServiceError(503, "DRAFT_UNAVAILABLE", "초안을 수정할 수 없다") from error
        # 0은 없거나 다른 ID인 초안, -1은 같은 ID가 다른 화면에서 수정된 초안이다.
        if result == 0:
            raise ServiceError(404, "DRAFT_NOT_FOUND", "작업 세션이 없거나 만료됐다")
        if result == -1:
            raise ServiceError(409, "DRAFT_CHANGED", "다른 화면의 초안과 내 입력을 비교한다")
        return DraftRecord(**state.model_dump(), ttl_seconds=self.ttl)

    async def clear(self, conversation_id: int, session_id: UUID, revision: UUID) -> str:
        try:
            result = await self.delete_script(keys=[self.key(conversation_id)],
                                               args=[str(session_id), str(revision)])
            return {-1: "changed", 0: "not_found", 1: "cleared"}[result]
        except RedisError:
            return "unavailable"

    async def clear_conversation(self, conversation_id: int) -> bool:
        try:
            await self.redis.delete(self.key(conversation_id))
            return True
        except RedisError:
            return False
