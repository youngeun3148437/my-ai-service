"""메시지 CRUD·페이지 조회 요청을 받고 서비스 결과를 응답한다."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.routers.dependencies import Chat
from app.schemas import Cleanup, MessageCreated, MessageInput, MessagePage, MessageUpdate, MessageWrite, PositiveId

router = APIRouter(tags=["메시지"])


# response_model은 서비스 결과에서 응답에 필요한 항목을 검증한다.
@router.get("/conversations/{conversation_id}/messages", response_model=MessagePage)
async def messages(conversation_id: PositiveId, service: Chat,
                   before_id: Annotated[int | None, Query(gt=0)] = None, refresh: bool = False):
    # 저장 성공 직후 UI는 refresh=true로 원본을 읽는다. 일반 반복 조회는 캐시를 쓴다.
    return await service.messages(conversation_id, before_id, refresh)


@router.post("/conversations/{conversation_id}/messages", response_model=MessageCreated, status_code=201)
async def create_message(conversation_id: PositiveId, body: MessageInput, service: Chat):
    return await service.create_message(conversation_id, body)


@router.patch("/messages/{message_id}", response_model=MessageWrite)
async def update_message(message_id: PositiveId, body: MessageUpdate, service: Chat):
    return await service.update_message(message_id, body.content)


@router.delete("/messages/{message_id}", response_model=Cleanup)
async def delete_message(message_id: PositiveId, service: Chat):
    return await service.delete_message(message_id)
