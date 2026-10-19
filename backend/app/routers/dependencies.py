"""현재 사용자와 공유 연결을 받아 데이터·캐시·초안 저장소를 서비스에 연결한다."""

from typing import Annotated

from fastapi import Depends

from app.core.dependencies import Authenticated, SharedResources
from app.services.chat import ChatService
from app.stores.cache import ConversationCache
from app.stores.database import Database
from app.stores.redis_state import WorkSessions


def get_chat_service(context: Authenticated, resources: SharedResources) -> ChatService:
    settings = resources.settings
    user_id = str(context.auth_user_id)
    # 객체는 요청별로 만들고, 내부 HTTP·Redis 연결은 공유한다.
    return ChatService(
        Database(context, attachments_enabled=settings.attachments_enabled,
                 cache_version_enabled=settings.cache_version_enabled),
        ConversationCache(resources.redis, user_id, settings.cache_ttl_seconds, settings.message_page_size),
        WorkSessions(resources.redis, user_id, settings.work_session_ttl_seconds),
        settings.message_page_size,
        cache_version_enabled=settings.cache_version_enabled,
    )


Chat = Annotated[ChatService, Depends(get_chat_service)]
