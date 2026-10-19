"""회원가입·로그인·토큰 갱신·로그아웃의 HTTP 경로를 제공한다."""

from fastapi import APIRouter, Response

from app.core.dependencies import AccessToken, AuthClient
from app.schemas import LoginInput, RefreshInput, SignupInput, SignupResponse, Tokens
from app.services import auth

router = APIRouter(prefix="/auth", tags=["인증"])


@router.post("/signup", response_model=SignupResponse, status_code=201)
async def signup(body: SignupInput, client: AuthClient):
    return await auth.signup(client, body)


@router.post("/login", response_model=Tokens)
async def login(body: LoginInput, client: AuthClient):
    return await auth.login(client, body)


@router.post("/refresh", response_model=Tokens)
async def refresh(body: RefreshInput, client: AuthClient):
    return await auth.refresh(client, body.refresh_token)


@router.post("/logout", status_code=204)
async def logout(token: AccessToken, client: AuthClient):
    await auth.logout(client, token)
    return Response(status_code=204)
