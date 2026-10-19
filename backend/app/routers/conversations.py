"""현재 사용자의 대화 CRUD와 이전 대화 페이지 요청을 연결한다."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.routers.dependencies import Chat
from app.schemas import Cleanup, ConversationInput, ConversationPage, ConversationRecord, PositiveId

router = APIRouter(tags=["대화"])


@router.post("/me/conversations", response_model=ConversationRecord, status_code=201)
async def create_conversation(body: ConversationInput, service: Chat):
    return await service.create_conversation(body.title)


@router.get("/me/conversations", response_model=ConversationPage)
async def conversations(service: Chat, before_id: Annotated[int | None, Query(gt=0)] = None):
    # before_id가 없으면 최신 페이지, 있으면 그 ID보다 이전 기록을 읽는다.
    return await service.conversations(before_id)


@router.patch("/conversations/{conversation_id}", response_model=ConversationRecord)
async def update_conversation(conversation_id: PositiveId, body: ConversationInput, service: Chat):
    return await service.update_conversation(conversation_id, body.title)


@router.delete("/conversations/{conversation_id}", response_model=Cleanup)
async def delete_conversation(conversation_id: PositiveId, service: Chat):
    return await service.delete_conversation(conversation_id)
