"""대화별 초안 저장·복원·수정·삭제의 HTTP 경로를 제공한다."""

from uuid import UUID

from fastapi import APIRouter, Response

from app.routers.dependencies import Chat
from app.schemas import ActiveDraft, DraftInput, DraftRecord, DraftUpdate, PositiveId

router = APIRouter(prefix="/conversations/{conversation_id}/work-sessions", tags=["작업 세션"])


@router.get("", response_model=ActiveDraft)
async def active_draft(conversation_id: PositiveId, service: Chat):
    return await service.active_draft(conversation_id)


@router.post("", response_model=DraftRecord, status_code=201)
async def create_draft(conversation_id: PositiveId, body: DraftInput, service: Chat):
    return await service.create_draft(conversation_id, body)


@router.get("/{session_id}", response_model=DraftRecord)
async def draft(conversation_id: PositiveId, session_id: UUID, service: Chat):
    return await service.draft(conversation_id, session_id)


@router.patch("/{session_id}", response_model=DraftRecord)
async def update_draft(conversation_id: PositiveId, session_id: UUID, body: DraftUpdate, service: Chat):
    return await service.update_draft(conversation_id, session_id, body)


@router.delete("/{session_id}", status_code=204)
async def delete_draft(conversation_id: PositiveId, session_id: UUID, revision: UUID, service: Chat):
    await service.delete_draft(conversation_id, session_id, revision)
    return Response(status_code=204)
