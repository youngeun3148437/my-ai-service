"""공유 자원을 주입하고 요청별 토큰·신원·Supabase 객체를 만든다."""

from dataclasses import dataclass, field
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from supabase import AsyncClient, acreate_client
from supabase.lib.client_options import AsyncClientOptions

from app.core.errors import ServiceError, auth_result
from app.core.resources import Resources

bearer = HTTPBearer(scheme_name="UserBearer", auto_error=False)


def get_resources(request: Request) -> Resources:
    return request.app.state.resources


# Annotated에 Depends를 붙이면 FastAPI가 호출 결과를 이 인자에 주입한다.
SharedResources = Annotated[Resources, Depends(get_resources)]


def get_token(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> str:
    if credentials is None:
        raise ServiceError(401, "LOGIN_REQUIRED", "로그인이 필요하다")
    return credentials.credentials


AccessToken = Annotated[str, Depends(get_token)]


async def make_client(resources: Resources, token: str | None = None) -> AsyncClient:
    # 요청 전용 헤더를 만든다. 아래 httpx_client는 서버가 관리하는 공유 연결 풀이다.
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await acreate_client(
        str(resources.settings.supabase_url).rstrip("/"),
        resources.settings.supabase_publishable_key,
        options=AsyncClientOptions(
            schema="chat_lab", headers=headers, httpx_client=resources.http,
            persist_session=False, auto_refresh_token=False,
        ),
    )
    # 객체와 헤더는 요청 전용이다. 공유 http.headers에 사용자 토큰을 넣지 않는다.


async def get_auth_client(resources: SharedResources) -> AsyncClient:
    # 이 객체는 연결 풀을 빌린다. 여기서 close하면 다른 요청의 연결도 닫힌다.
    return await make_client(resources)


AuthClient = Annotated[AsyncClient, Depends(get_auth_client)]


# frozen=True는 확인된 사용자 정보를 요청 처리 중 다시 대입하지 못하게 한다.
@dataclass(frozen=True)
class UserContext:
    auth_user_id: UUID
    client: AsyncClient
    # Storage에도 검증된 같은 JWT를 보낸다. 객체의 repr에는 토큰을 표시하지 않는다.
    access_token: str = field(default='', repr=False)


async def get_user_context(token: AccessToken, resources: SharedResources) -> UserContext:
    client = await make_client(resources, token)
    result = await auth_result(client.auth.get_user(token))
    if result is None or result.user is None:
        raise ServiceError(401, "AUTH_REJECTED", "Access token을 확인한다")
    # 동일 요청에서는 이 검증 결과를 재사용하고, Data API에도 같은 토큰을 보낸다.
    return UserContext(UUID(str(result.user.id)), client, token)


Authenticated = Annotated[UserContext, Depends(get_user_context)]
