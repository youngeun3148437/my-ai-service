"""최근 메시지 복사본의 DB 버전을 검사하고 Redis 실패 시 원본 조회를 계속한다."""
import logging

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis import RedisError
from redis.asyncio import Redis

from app.core.errors import ServiceError
from app.schemas import MessagePage

logger = logging.getLogger("course.cache")


class CacheEntry(BaseModel):
    # strict는 True나 "7"을 버전 번호로 바꾸지 않는다. PostgreSQL bigint 범위다.
    model_config = ConfigDict(extra="forbid")
    version: int = Field(strict=True, ge=0, le=9223372036854775807)
    page: MessagePage


class ConversationCache:
    def __init__(self, redis: Redis, user_id: str, ttl: int, page_size: int = 30):
        self.redis, self.user_id, self.ttl, self.page_size = redis, user_id, ttl, page_size

    def key(self, conversation_id: int) -> str:
        # 사용자·대화·페이지 크기를 구분해 다른 조회 결과가 같은 키에 섞이지 않게 한다.
        return f"course:conversation-cache:{self.user_id}:{conversation_id}:{self.page_size}"

    def valid_page(self, conversation_id: int, page: MessagePage) -> bool:
        # Redis에는 RLS가 없다. 같은 대화·페이지 크기·ID 순서인지도 검사한다.
        ids = [message.id for message in page.messages]
        return (
            len(ids) <= self.page_size
            and all(message.conversation_id == conversation_id for message in page.messages)
            and all(value > 0 for value in ids)
            and ids == sorted(set(ids))
            and (page.next_before_id is None or len(ids) == self.page_size and page.next_before_id == ids[0])
        )

    async def read(self, conversation_id: int, version: int | None = None) -> tuple[MessagePage | None, bool]:
        try:
            raw = await self.redis.get(self.key(conversation_id))
            if raw is None:
                return None, False
            if version is None:
                # 기존 단순 페이지 형식도 읽을 수 있다. 빈 목록 역시 캐시할 수 있다.
                page = MessagePage.model_validate_json(raw)
            else:
                page = self.versioned_page(conversation_id, raw, version)
                if page is None:
                    return None, False
            return page.model_copy(update={"source": "cache"}), False
        except (RedisError, ValueError, ValidationError):
            logger.warning("cache_read_failed conversation_id=%s", conversation_id)
            return None, True

    async def write(self, conversation_id: int, page: MessagePage, version: int | None = None) -> bool:
        try:
            if version is not None:
                return await self.write_versioned_page(conversation_id, page, version)
            # ex는 저장과 만료 시간 설정을 SET 한 명령으로 처리한다.
            await self.redis.set(self.key(conversation_id), page.model_dump_json(), ex=self.ttl)
            return True
        except RedisError:
            logger.warning("cache_write_failed conversation_id=%s", conversation_id)
            return False

    async def clear(self, conversation_id: int) -> bool:
        try:
            await self.redis.delete(self.key(conversation_id))
            return True
        except RedisError:
            logger.warning("cache_clear_failed conversation_id=%s", conversation_id)
            return False

    def versioned_page(self, conversation_id: int, raw, version: int) -> MessagePage | None:
        # 현재 DB 버전·대화·페이지 범위에 맞는 캐시만 사용한다.
        entry = CacheEntry.model_validate_json(raw)
        if entry.version != version:
            return None
        if not self.valid_page(conversation_id, entry.page):
            raise ValueError("캐시의 대화·페이지 범위가 잘못됐다")
        return entry.page

    async def write_versioned_page(self, conversation_id: int, page: MessagePage, version: int) -> bool:
        # 확인한 버전과 페이지를 Redis SET·TTL에 연결한다.
        raw = CacheEntry(version=version, page=page).model_dump_json()
        await self.redis.set(self.key(conversation_id), raw, ex=self.ttl)
        return True
