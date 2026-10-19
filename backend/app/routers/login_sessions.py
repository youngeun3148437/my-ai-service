"""브라우저 로그인 세션용 경로를 기존 Bearer API와 분리해 제공한다."""

import base64
import hashlib
import hmac
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, SecretStr

from app.core.dependencies import AccessToken, AuthClient, SharedResources
from app.core.errors import ServiceError
from app.services.login_sessions import LoginSessionService, get_login_session_settings
from app.stores.browser_auth import BrowserAuthStore
from app.stores.login_sessions import LoginSessionStore

def require_client(request: Request):
    config = get_login_session_settings()
    supplied = request.headers.get('x-login-client-secret', '')
    # 브라우저는 이 값을 받지 않는다. 화면 Python 서버만 코드·세션을 교환할 수 있다.
    expected = config.login_client_secret.get_secret_value()
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise ServiceError(403, 'LOGIN_CLIENT_REJECTED', '로그인 서버 연결 설정을 확인한다')


router = APIRouter(prefix='/auth/browser-sessions', tags=['브라우저 로그인 복원'],
                   dependencies=[Depends(require_client)])


class SessionAccess(BaseModel):
    session_id: str = Field(pattern=r'^[A-Za-z0-9_-]{43}$', repr=False)
    user_id: UUID
    access_token: str = Field(repr=False)
    expires_in: int = Field(gt=0)
    session_seconds: int = Field(gt=0)


class RestoreInput(BaseModel):
    force: bool = False
    previous_access_token: SecretStr | None = None


class ExchangeInput(BaseModel):
    code: str = Field(pattern=r'^[A-Za-z0-9_-]{43}$', repr=False)
    code_verifier: str = Field(pattern=r'^[A-Za-z0-9._~-]{43,128}$', repr=False)
    redirect_uri: str = Field(max_length=2048)
    state: str = Field(pattern=r'^[A-Za-z0-9_-]{43}$', repr=False)


class LogoutTicketInput(BaseModel):
    redirect_uri: str = Field(max_length=2048)
    state: str = Field(pattern=r'^[A-Za-z0-9_-]{43}$', repr=False)


def service(resources: SharedResources, client: AuthClient) -> LoginSessionService:
    config = get_login_session_settings()
    if not config.login_sessions_enabled:
        raise ServiceError(503, 'LOGIN_RESTORE_DISABLED', '로그인 복원 설정을 확인한다')
    store = LoginSessionStore(resources.redis, config.login_session_encryption_key.get_secret_value(),
                              resources.settings.redis_timeout_seconds)
    return LoginSessionService(store, client, config.login_session_seconds)


Sessions = Annotated[LoginSessionService, Depends(service)]


def no_store(response: Response):
    # 인증 응답은 공유 캐시에 저장하지 않는다.
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Pragma'] = 'no-cache'


@router.post('/exchange', response_model=SessionAccess)
async def exchange(body: ExchangeInput, response: Response, sessions: Sessions):
    from app.routers.browser_auth import allow_redirect
    config = get_login_session_settings()
    allow_redirect(body.redirect_uri, config)
    no_store(response)
    values = await BrowserAuthStore(sessions.store).read('code', body.code, consume=True)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(body.code_verifier.encode()).digest()).rstrip(b'=').decode()
    if (not hmac.compare_digest(values['code_challenge'], challenge)
            or not hmac.compare_digest(values['state'], body.state)
            or values['redirect_uri'] != body.redirect_uri):
        raise ServiceError(400, 'LOGIN_CODE_REJECTED', '로그인을 시작한 화면에서 다시 요청한다')
    return await sessions.restore(values['session_id'])


@router.post('/restore', response_model=SessionAccess)
async def restore(body: RestoreInput, response: Response, identifier: AccessToken, sessions: Sessions):
    # 이 경로의 Bearer 값은 로그인 세션 식별자다. 다른 보호 API는 계속 Supabase JWT를 받는다.
    no_store(response)
    previous = body.previous_access_token.get_secret_value() if body.previous_access_token else None
    return await sessions.restore(identifier, force=body.force, previous_access_token=previous)


@router.post('/logout')
async def logout(response: Response, identifier: AccessToken, sessions: Sessions):
    no_store(response)
    return await sessions.logout(identifier)


@router.post('/logout-ticket')
async def logout_ticket(body: LogoutTicketInput, response: Response, identifier: AccessToken, sessions: Sessions):
    from app.routers.browser_auth import allow_redirect
    from app.stores.login_sessions import session_keys
    allow_redirect(body.redirect_uri, get_login_session_settings())
    session_keys(identifier)
    no_store(response)
    # 서버 세션부터 폐기한다. 다음 브라우저 이동은 해당 쿠키를 정리하는 작업이다.
    await sessions.logout(identifier)
    ticket = await BrowserAuthStore(sessions.store).issue('logout', {
        'session_id': identifier, 'redirect_uri': body.redirect_uri, 'state': body.state})
    return {'ticket': ticket}
