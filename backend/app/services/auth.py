"""Supabase Auth 호출 결과를 가입 응답·토큰 응답으로 정리한다."""

from supabase import AsyncClient

from app.core.errors import ServiceError, auth_result
from app.schemas import LoginInput, SignupInput


def tokens(result) -> dict:
    # Supabase 응답에서 사용자·새 토큰 쌍·수명만 꺼내 API의 응답으로 만든다.
    if result.user is None or result.session is None:
        raise ServiceError(401, "AUTH_REJECTED", "로그인 결과를 확인할 수 없다")
    return {"user_id": result.user.id, "access_token": result.session.access_token,
            "refresh_token": result.session.refresh_token, "expires_in": result.session.expires_in}


async def signup(client: AsyncClient, body: SignupInput) -> dict:
    # model_dump(mode="json")은 요청 모델을 SDK에 전달할 dict로 바꾼다.
    result = await auth_result(client.auth.sign_up(body.model_dump(mode="json")), signup=True)
    if result.user is None:
        raise ServiceError(502, "AUTH_UNAVAILABLE", "가입 결과를 확인할 수 없다")
    return {"user_id": result.user.id, "email_confirmation_required": result.session is None}


async def login(client: AsyncClient, body: LoginInput) -> dict:
    return tokens(await auth_result(client.auth.sign_in_with_password(body.model_dump(mode="json"))))


async def refresh(client: AsyncClient, refresh_token: str) -> dict:
    # SDK에 로그인 상태를 저장하지 않으므로 갱신할 토큰을 명시한다.
    # 입력은 refresh_token이다. auth_result는 SDK 오류를 변환하고 tokens는 새 토큰 쌍·수명을 반환한다.
    # 갱신 토큰을 보내 새 토큰 쌍을 받는다.
    return tokens(await auth_result(client.auth.refresh_session(refresh_token)))


async def logout(client: AsyncClient, access_token: str) -> None:
    # Publishable key와 사용자 JWT로 해당 세션만 종료한다. Secret key를 사용하지 않는다.
    await auth_result(client.auth.admin.sign_out(access_token, scope="local"))
