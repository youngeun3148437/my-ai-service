"""현재 사용자의 프로필 생성·조회·수정 요청을 서비스에 전달한다."""

from fastapi import APIRouter

from app.routers.dependencies import Chat
from app.schemas import ProfileInput, ProfileRecord

router = APIRouter(tags=["프로필"])


@router.post("/me/profile", response_model=ProfileRecord, status_code=201)
async def create_profile(body: ProfileInput, service: Chat):
    return await service.create_profile(body.display_name)


@router.get("/me", response_model=ProfileRecord)
async def profile(service: Chat):
    return await service.profile()


@router.patch("/me", response_model=ProfileRecord)
async def update_profile(body: ProfileInput, service: Chat):
    return await service.update_profile(body.display_name)
