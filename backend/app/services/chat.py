"""사용자 데이터 처리와 DB 원본·Redis 초안·캐시의 실행 순서를 연결한다."""

from uuid import UUID

from app.core.errors import ServiceError
from app.schemas import (
    ActiveDraft, Cleanup, ConversationPage, DraftInput, DraftUpdate, MessageCreated,
    MessageInput, MessagePage, MessageWrite,
)
from app.stores.cache import ConversationCache
from app.stores.database import Database
from app.stores.redis_state import WorkSessions


class ChatService:
    def __init__(self, db: Database, cache: ConversationCache, sessions: WorkSessions, page_size: int,
                 cache_version_enabled: bool = False):
        self.db, self.cache, self.sessions, self.page_size = db, cache, sessions, page_size
        self.cache_version_enabled = cache_version_enabled

    async def profile(self) -> dict:
        return await self.db.profile()

    async def create_profile(self, name: str) -> dict:
        return await self.db.create_profile(name)

    async def update_profile(self, name: str) -> dict:
        return await self.db.update_profile(name)

    async def conversations(self, before_id: int | None) -> ConversationPage:
        rows = await self.db.conversations(before_id, self.page_size + 1)
        selected = rows[:self.page_size]
        return ConversationPage(conversations=selected,
                                next_before_id=selected[-1]["id"] if len(rows) > self.page_size else None)

    async def create_conversation(self, title: str) -> dict:
        return await self.db.create_conversation(title)

    async def update_conversation(self, conversation_id: int, title: str) -> dict:
        return await self.db.update_conversation(conversation_id, title)

    async def delete_conversation(self, conversation_id: int) -> Cleanup:
        # DB 삭제 성공 후에만 Redis를 정리한다. 남은 임시 값에는 TTL이 적용된다.
        await self.db.delete_conversation(conversation_id)
        return Cleanup(cache_cleared=await self.cache.clear(conversation_id),
                       drafts_cleared=await self.sessions.clear_conversation(conversation_id))

    async def messages(self, conversation_id: int, before_id: int | None, refresh: bool = False) -> MessagePage:
        # Redis는 RLS를 실행하지 않는다. 캐시를 읽기 전에도 현재 소유권을 확인한다.
        conversation = await self.db.conversation(conversation_id)
        if self.cache_version_enabled:
            if before_id is not None:
                return await self.older_messages(conversation_id, before_id)
            return await self.versioned_messages(conversation_id, self.cache_version(conversation), refresh)
        fallback = False
        if before_id is None and not refresh:
            # 최신 페이지에만 캐시를 사용한다. 이전 페이지는 ID로 DB에서 이어 읽는다.
            page, fallback = await self.cache.read(conversation_id)
            if page is not None:
                return page
        rows = await self.db.messages(conversation_id, before_id, self.page_size)
        page = self.message_page(rows, fallback)
        if before_id is None:
            written = await self.cache.write(conversation_id, page)
            if not written:
                page.source = "supabase_fallback"
        return page

    @staticmethod
    def cache_version(conversation: dict) -> int:
        version = conversation.get("cache_version")
        # bool은 int의 하위 자료형이므로 type으로 구분한다. 미설정 DB를 캐시로 대체하지 않는다.
        if type(version) is not int or not 0 <= version <= 9223372036854775807:
            raise ServiceError(502, "CACHE_VERSION_UNAVAILABLE", "캐시 버전 SQL과 CACHE_VERSION_ENABLED 설정을 확인한다")
        return version

    def message_page(self, rows: list[dict], fallback: bool = False) -> MessagePage:
        selected = rows[:self.page_size]
        next_id = selected[-1]["id"] if len(rows) > self.page_size else None
        # DB는 최신 ID부터 조회한다. 화면의 한 페이지는 오래된 메시지부터 표시한다.
        return MessagePage(messages=list(reversed(selected)), next_before_id=next_id,
                           source="supabase_fallback" if fallback else "supabase")

    async def versioned_messages(self, conversation_id: int, version: int, refresh: bool) -> MessagePage:
        fallback = False
        if not refresh:
            page, fallback = await self.cache.read(conversation_id, version)
            if page is not None:
                # 캐시 조회 뒤 DB 소유권·버전을 다시 읽고 비교한다.
                confirmed = self.cache_version(await self.db.conversation(conversation_id))
                if confirmed == version:
                    return page
                version = confirmed
        # DB 페이지를 읽는 동안 쓰기가 발생하면 새 버전으로 한 번만 다시 읽는다.
        for _ in range(2):
            rows = await self.db.messages(conversation_id, None, self.page_size)
            page = self.message_page(rows, fallback)
            confirmed = self.cache_version(await self.db.conversation(conversation_id))
            if confirmed == version:
                # 검증 뒤 늦게 SET해도 원래 버전으로 저장한다. 다음 요청이 낡은 버전을 거른다.
                if not await self.cache.write(conversation_id, page, version):
                    page.source = "supabase_fallback"
                return page
            version = confirmed
        # 계속 바뀌면 마지막 DB 조회 결과만 반환하고 캐시는 채우지 않는다.
        # 여러 HTTP 조회 사이에 변경이 가능하므로 응답 순간의 최신 상태까지 보장하지 않는다.
        return page

    async def create_message(self, conversation_id: int, body: MessageInput) -> MessageCreated:
        revision, skipped = body.session_revision, None
        if body.session_id is not None and revision is None:
            # 이전 클라이언트도 지원한다. INSERT 전에 읽은 버전만 저장 성공 후 정리한다.
            try:
                revision = (await self.sessions.read(conversation_id, body.session_id)).revision
            except ServiceError as error:
                if error.status_code not in (404, 503):
                    raise
                skipped = 'not_found' if error.status_code == 404 else 'unavailable'
        # 최신 입력을 저장한다. 초안 ID·버전은 정리 조건이며 INSERT 본문에서 제외한다.
        values = {"role": body.role, "content": body.content}
        # UUID 없는 기존 요청은 INSERT를 유지한다. UUID 있는 요청만 아래 접수 함수로 보낸다.
        if body.client_request_id is None:
            row, replayed = await self.db.create_message(conversation_id, values), False
        else:
            row, replayed = await self.requested_message(conversation_id, values, body.client_request_id)
        # DB 실패 시 아래 정리는 실행되지 않는다. Redis 실패는 저장 성공을 취소하지 않는다.
        cache_cleared = await self.cache.clear(conversation_id)
        draft_cleanup = None
        if body.session_id is not None:
            # 같은 ID·버전만 지운다. 만료는 전송을 막지 않고 다른 화면의 수정본은 보존한다.
            draft_cleanup = skipped or await self.sessions.clear(conversation_id, body.session_id, revision)
        return MessageCreated(message=row, cache_cleared=cache_cleared, draft_cleanup=draft_cleanup, replayed=replayed)

    async def update_message(self, message_id: int, content: str) -> MessageWrite:
        row = await self.db.update_message(message_id, content)
        return MessageWrite(message=row, cache_cleared=await self.cache.clear(row["conversation_id"]))

    async def delete_message(self, message_id: int) -> Cleanup:
        row = await self.db.delete_message(message_id)
        return Cleanup(cache_cleared=await self.cache.clear(row["conversation_id"]))

    async def create_draft(self, conversation_id: int, body: DraftInput):
        await self.db.conversation(conversation_id)
        return await self.sessions.create(conversation_id, body.draft_question)

    async def active_draft(self, conversation_id: int) -> ActiveDraft:
        # Redis 자체에는 RLS가 없다. 대화 소유권 확인 후 사용자별 키를 읽는다.
        await self.db.conversation(conversation_id)
        return ActiveDraft(draft=await self.sessions.active(conversation_id))

    async def draft(self, conversation_id: int, session_id: UUID):
        await self.db.conversation(conversation_id)
        return await self.sessions.read(conversation_id, session_id)

    async def update_draft(self, conversation_id: int, session_id: UUID, body: DraftUpdate):
        await self.db.conversation(conversation_id)
        return await self.sessions.update(conversation_id, session_id, body.draft_question, body.revision)

    async def delete_draft(self, conversation_id: int, session_id: UUID, revision: UUID):
        # 자동 빈 입력 정리도 같은 API를 사용한다. 성공한 버전만 삭제하고 실패 종류를 구분한다.
        await self.db.conversation(conversation_id)
        status = await self.sessions.clear(conversation_id, session_id, revision)
        if status in {'changed', 'not_found', 'unavailable'}:
            self.draft_delete_error(status)

    @staticmethod
    def draft_delete_error(status: str) -> None:
        # 삭제 실패를 충돌·부재·연결 오류로 구분해 화면의 입력 보존과 비교에 연결한다.
        if status == 'changed':
            raise ServiceError(409, 'DRAFT_CHANGED', '다른 화면이 수정한 초안을 먼저 확인한다')
        if status == 'not_found':
            raise ServiceError(404, 'DRAFT_NOT_FOUND', '작업 세션이 없거나 만료됐다')
        if status == 'unavailable':
            raise ServiceError(503, 'DRAFT_UNAVAILABLE', '초안을 삭제할 수 없다')


    async def requested_message(self, conversation_id: int, values: dict, request_id: UUID) -> tuple[dict, bool]:
        # 최초 본문과 UUID를 그대로 DB 접수에 넘겨 (저장 원본, 재사용 여부)를 받는다.
        # 캐시·초안 정리와 MessageCreated 응답은 호출한 create_message가 저장 성공 뒤 처리한다.
        # UUID가 있는 전송을 원자적 접수에 연결한다. UUID가 없으면 기존 INSERT를 유지한다.
        return await self.db.create_message_once(conversation_id, values, request_id)

    async def older_messages(self, conversation_id: int, before_id: int) -> MessagePage:
        # 이전 페이지는 항상 DB에서 읽는다. 버전 캐시는 최신 페이지에만 적용한다.
        rows = await self.db.messages(conversation_id, before_id, self.page_size)
        page = self.message_page(rows)
        ids = [message.id for message in page.messages]
        if (ids != sorted(set(ids)) or any(value >= before_id for value in ids)
                or page.next_before_id is not None and (not ids or page.next_before_id != ids[0])):
            self.older_page_error()
        return page

    @staticmethod
    def older_page_error() -> None:
        # 이전 페이지의 ID·커서가 요청 범위를 벗어나면 표시하지 않고 조회 오류를 전달한다.
        raise ServiceError(502, 'MESSAGE_PAGE_UNAVAILABLE', '이전 기록의 ID 순서와 커서 범위를 확인한다')
