"""브라우저가 직접 방문해 HttpOnly 쿠키를 받는다. 화면 서버에는 일회용 코드만 전달한다."""

import hmac
import secrets
from typing import Annotated
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from app.core.dependencies import AuthClient
from app.core.errors import ServiceError
from app.routers.browser_auth_html import auth_card, logout_card
from app.routers.login_sessions import Sessions
from app.schemas import LoginInput, SignupInput
from app.services import auth
from app.services.login_sessions import LoginSessionSettings, get_login_session_settings
from app.stores.browser_auth import BrowserAuthStore
from app.stores.login_sessions import SESSION_PATTERN

router = APIRouter(prefix='/auth/browser', tags=['브라우저 인증'], include_in_schema=False)
COOKIE_NAME = 'ai_chat_session_v2'
FLOW_PREFIX = 'ai_chat_flow_v2_'
Config = Annotated[LoginSessionSettings, Depends(get_login_session_settings)]


def allow_redirect(value: str, config: LoginSessionSettings) -> str:
    if value not in config.login_redirect_urls:
        raise ServiceError(400, 'LOGIN_REDIRECT_REJECTED', '등록된 화면 주소로 인증을 시작한다')
    return value


def browser_transport(request: Request, config: LoginSessionSettings):
    # 실제 배포는 HTTPS만 허용한다. Secure 해제는 개인 PC의 루프백 주소에만 쓴다.
    if config.login_cookie_secure:
        valid = request.url.scheme == 'https'
    else:
        valid = request.url.scheme == 'http' and request.url.hostname in {'localhost', '127.0.0.1', '::1'}
    if not valid:
        raise ServiceError(400, 'LOGIN_TRANSPORT_REJECTED', '인증 서버의 HTTPS·로컬 쿠키 설정을 확인한다')


def same_origin(request: Request):
    expected = f'{request.url.scheme}://{request.url.netloc}'
    origin = request.headers.get('origin')
    if not origin:
        try:
            referer = urlsplit(request.headers.get('referer', ''))
            origin = f'{referer.scheme}://{referer.netloc}'
        except ValueError:
            origin = ''
    if origin != expected:
        raise ServiceError(403, 'LOGIN_CSRF_REJECTED', '인증 화면에서 다시 요청한다')


def unique_query(request: Request):
    # 중간 프록시와 앱이 서로 다른 쿼리 값을 선택하지 않도록 중복 인증 인자를 거절한다.
    for name in ('redirect_uri', 'state', 'code_challenge', 'ticket'):
        if len(request.query_params.getlist(name)) > 1:
            raise ServiceError(400, 'LOGIN_QUERY_REJECTED', '인증 주소를 다시 연다')


async def form_values(request: Request) -> dict:
    if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
        raise ServiceError(415, 'LOGIN_FORM_REQUIRED', '인증 폼으로 요청한다')
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 16 * 1024:
            raise ServiceError(413, 'LOGIN_FORM_TOO_LARGE', '인증 입력 길이를 확인한다')
        data.extend(chunk)
    try:
        values = parse_qs(data.decode(), keep_blank_values=True, max_num_fields=10)
        if any(len(items) != 1 for items in values.values()):
            raise ValueError()
        return {key: items[0] for key, items in values.items()}
    except (UnicodeDecodeError, ValueError):
        raise ServiceError(400, 'LOGIN_FORM_REQUIRED', '인증 폼으로 다시 요청한다') from None


def redirect(values: dict, **params) -> RedirectResponse:
    response = RedirectResponse(values['redirect_uri'] + '?' + urlencode({**params, 'login_state': values['state']}),
                                status_code=303)
    response.headers.update({'Cache-Control': 'private, no-store', 'Pragma': 'no-cache', 'Referrer-Policy': 'no-referrer'})
    return response


def cookie(response, name: str, value: str, seconds: int, config: LoginSessionSettings, *, path='/'):
    response.set_cookie(name, value, max_age=seconds, path=path, secure=config.login_cookie_secure,
                        httponly=True, samesite='lax')


async def completed(values: dict, access: dict, sessions, config):
    code = await BrowserAuthStore(sessions.store).issue('code', {**values, 'session_id': access['session_id']})
    response = redirect(values, login_code=code)
    cookie(response, COOKIE_NAME, access['session_id'], access['session_seconds'], config)
    return response


@router.get('/authorize', dependencies=[Depends(unique_query)])
async def authorize(request: Request, sessions: Sessions, config: Config,
                    redirect_uri: str, state: Annotated[str, Query(pattern=r'^[A-Za-z0-9_-]{43}$')],
                    code_challenge: Annotated[str, Query(pattern=r'^[A-Za-z0-9_-]{43}$')]):
    browser_transport(request, config)
    values = {'redirect_uri': allow_redirect(redirect_uri, config), 'state': state, 'code_challenge': code_challenge}
    identifier = request.cookies.get(COOKIE_NAME)
    if identifier and SESSION_PATTERN.fullmatch(identifier):
        try:
            access = await sessions.restore(identifier)
            return await completed(values, access, sessions, config)
        except ServiceError as error:
            if error.status_code not in (401, 403):
                raise
    csrf = secrets.token_urlsafe(32)
    flow = await BrowserAuthStore(sessions.store).issue('flow', {**values, 'csrf': csrf}, 600)
    response = auth_card(flow, csrf, redirect_uri=values['redirect_uri'])
    cookie(response, FLOW_PREFIX + flow, csrf, 600, config, path='/auth/browser')
    return response


@router.post('/submit')
async def submit(request: Request, sessions: Sessions, config: Config, client: AuthClient):
    browser_transport(request, config)
    same_origin(request)
    body = await form_values(request)
    store = BrowserAuthStore(sessions.store)
    flow = body.get('flow', '')
    values = await store.read('flow', flow)
    allow_redirect(values['redirect_uri'], config)
    csrf = body.get('csrf', '')
    if (not csrf or not hmac.compare_digest(csrf.encode(), values['csrf'].encode())
            or not hmac.compare_digest(csrf.encode(), request.cookies.get(FLOW_PREFIX + flow, '').encode())):
        raise ServiceError(403, 'LOGIN_CSRF_REJECTED', '인증 화면을 다시 연다')
    action = body.get('action')
    email = body.get('email', '')[:320]
    if action in {'switch_signup', 'switch_login'}:
        return auth_card(flow, csrf, redirect_uri=values['redirect_uri'], signup=action == 'switch_signup', email=email)
    if action == 'cancel':
        await store.read('flow', flow, consume=True)
        result = redirect(values, login_error='cancelled')
    elif action in {'login', 'signup'}:
        try:
            if action == 'signup':
                signup_body = SignupInput(email=email, password=body.get('password', ''))
                if signup_body.password != body.get('confirm'):
                    return auth_card(flow, csrf, redirect_uri=values['redirect_uri'], signup=True, email=email, message='비밀번호 확인이 일치하지 않는다.')
                signup_result = await auth.signup(client, signup_body)
                message = ('가입 요청을 처리했다. 받은 확인 메일이 있으면 확인한 뒤 로그인한다.'
                           if signup_result['email_confirmation_required'] else '가입 요청을 처리했다. 로그인해 사용한다.')
                return auth_card(flow, csrf, redirect_uri=values['redirect_uri'], email=email, message=message)
            access = await sessions.login(LoginInput(email=email, password=body.get('password', '')))
        except ValidationError:
            return auth_card(flow, csrf, redirect_uri=values['redirect_uri'], signup=action == 'signup', email=email,
                             message='이메일과 비밀번호 조건을 확인한다.')
        except ServiceError as error:
            return auth_card(flow, csrf, redirect_uri=values['redirect_uri'], signup=action == 'signup', email=email,
                             message=error.detail.get('message', '인증 요청을 다시 확인한다.'))
        # 성공한 흐름만 소모한다. 이전 브라우저 세션이 있으면 새 계정으로 교체할 때 폐기한다.
        try:
            await store.read('flow', flow, consume=True)
            old = request.cookies.get(COOKIE_NAME)
            if old and old != access['session_id'] and SESSION_PATTERN.fullmatch(old):
                await sessions.logout(old)
            result = await completed(values, access, sessions, config)
        except ServiceError:
            # 동시에 제출한 다른 요청이 먼저 흐름을 완료했다면 미사용 새 세션도 폐기한다.
            try:
                await sessions.logout(access['session_id'])
            except ServiceError:
                pass
            raise
    else:
        raise ServiceError(400, 'LOGIN_FORM_REQUIRED', '인증 폼으로 다시 요청한다')
    result.delete_cookie(FLOW_PREFIX + flow, path='/auth/browser', secure=config.login_cookie_secure,
                         httponly=True, samesite='lax')
    return result


@router.get('/logout', dependencies=[Depends(unique_query)])
async def logout_page(request: Request, ticket: str, sessions: Sessions, config: Config):
    browser_transport(request, config)
    values = await BrowserAuthStore(sessions.store).read('logout', ticket)
    allow_redirect(values['redirect_uri'], config)
    # GET은 쿠키·서버 세션을 삭제하지 않는다. 같은 출처의 POST 폼으로만 이동한다.
    return logout_card(ticket, secrets.token_urlsafe(24), redirect_uri=values['redirect_uri'])


@router.post('/logout')
async def logout_complete(request: Request, sessions: Sessions, config: Config):
    browser_transport(request, config)
    same_origin(request)
    body = await form_values(request)
    values = await BrowserAuthStore(sessions.store).read('logout', body.get('ticket', ''), consume=True)
    allow_redirect(values['redirect_uri'], config)
    result = redirect(values, login_error='logged_out')
    # 오래된 탭의 티켓으로 새로 로그인한 다른 계정의 쿠키를 지우지 않는다.
    current = request.cookies.get(COOKIE_NAME, '')
    if hmac.compare_digest(current.encode(), values['session_id'].encode()):
        result.delete_cookie(COOKIE_NAME, path='/', secure=config.login_cookie_secure,
                             httponly=True, samesite='lax')
    return result
