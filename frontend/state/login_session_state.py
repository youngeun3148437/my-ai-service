"""HttpOnly 인증 왕복·서버 전용 코드 교환·주기 검증을 화면 수명에 연결한다."""

import re
from time import monotonic
from urllib.parse import urlencode
from uuid import UUID, uuid4

import streamlit as st

from clients.api_client import ApiError
from clients.browser_auth import browser_auth
from clients.login_exchange import LoginExchange
from clients.login_session_client import LoginSessionClient
from config.login_config import app_public_url, login_client_secret

SESSION_PATTERN = re.compile(r'^[A-Za-z0-9_-]{43}$')
SESSION_CHECK_SECONDS = 30
ACCESS_REFRESH_MARGIN_SECONDS = 30


def operation(state, op='read', **values):
    # lr_는 계정 화면의 ui_ 상태와 분리한다. JS에는 짧은 거래 정보와 이동 주소만 보낸다.
    state['lr_operation'] = dict(op=op, nonce=uuid4().hex, redirect_uri=app_public_url(), **values)


def begin_login(base_url: str, state, *, reason='login'):
    state['lr_checked'] = True
    state['lr_auth_error'] = None
    operation(state, 'authorize', authorize_url=base_url.rstrip('/') + '/auth/browser/authorize', reason=reason)


def auth_button(base_url: str, *, logout=False):
    """사용자 클릭에서 새 인증 창을 연다. Python 왕복 뒤에는 팝업을 처음 열지 않는다."""
    state = st.session_state
    slot = 'logout_action' if logout else 'login_action'
    key = 'lr_' + slot + '_operation'
    if key not in state:
        state[key] = dict(op='logout_button' if logout else 'authorize_button', nonce=uuid4().hex,
            redirect_uri=app_public_url(), authorize_url=base_url.rstrip('/') + '/auth/browser/authorize',
            reason='login', label='로그아웃' if logout else '로그인 · 회원가입', primary=not logout)
    receipt = browser_auth(state[key], slot=slot).receipt
    if not receipt:
        return
    if receipt.get('kind') == 'logout_ready' and state.get('lr_logout_started') != receipt.get('state'):
        state['lr_logout_started'] = receipt['state']
        finish_logout(base_url, state, receipt)
    if receipt.get('kind') == 'popup_blocked':
        st.warning('브라우저에서 이 사이트의 팝업을 허용한 뒤 버튼을 다시 누른다.')


def accept_access(state, result: dict, now: float):
    try:
        UUID(str(result['user_id']))
        if (not isinstance(result['session_id'], str) or not SESSION_PATTERN.fullmatch(result['session_id'])
                or not isinstance(result['access_token'], str) or not result['access_token']
                or type(result['expires_in']) is not int or result['expires_in'] <= 0
                or type(result['session_seconds']) is not int or result['session_seconds'] <= 0):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ApiError(502, 'AUTH_RESPONSE_ERROR', '로그인 복원 응답을 확인한다.') from None
    owner = state.get('ui_owner_id')
    if owner and str(owner) != str(result['user_id']):
        raise ApiError(401, 'AUTH_SESSION_ENDED', '로그인 사용자가 달라졌다.')
    # 세션 ID·접근 토큰은 Python 사용자별 상태에만 둔다. JS 컴포넌트 인수에 넣지 않는다.
    state['lr_session_id'] = result['session_id']
    state['ui_owner_id'] = str(result['user_id'])
    state['ui_tokens'] = {key: result[key] for key in ('user_id', 'access_token', 'expires_in')}
    state['ui_token_deadline'] = now + result['expires_in']
    state['ui_refresh_retry_after'] = 0.0
    state['lr_checked'] = True
    state['lr_verified_at'] = now
    if state.get('lr_periodic_session_id') != result['session_id']:
        state['lr_periodic_session_id'] = result['session_id']
        state['lr_periodic_check_due'] = now + SESSION_CHECK_SECONDS


def invalidate(state):
    from state.auth_state import end_session
    end_session(state)
    state['lr_session_id'] = None
    state['lr_checked'] = True
    state['lr_auth_error'] = '로그인 세션이 끝났다. 다시 로그인한다.'
    operation(state)


def renew_persistent(base_url: str, state, *, force=False, verify=False, clock=None) -> bool:
    now = (clock or monotonic)()
    if not force and now < state.get('ui_refresh_retry_after', 0):
        raise ApiError(503, 'AUTH_REFRESH_UNAVAILABLE', '인증 연결을 잠시 뒤 다시 확인한다.')
    verified_at = state.get('lr_verified_at')
    if (not force and not verify and verified_at is not None and now < verified_at + SESSION_CHECK_SECONDS
            and state.get('ui_token_deadline', 0) - now > ACCESS_REFRESH_MARGIN_SECONDS):
        return False
    identifier = state.get('lr_session_id')
    previous = (state.get('ui_tokens') or {}).get('access_token')
    try:
        # 서버 비밀 헤더는 이 Python HTTP 요청에만 붙인다. 데이터 쓰기 요청은 재전송하지 않는다.
        result = LoginSessionClient(base_url, identifier).request('POST', '/auth/browser-sessions/restore',
            body={'force': force, 'previous_access_token': previous if force else None})
        accept_access(state, result, now)
    except ApiError as error:
        if error.status in (401, 403) or error.code == 'AUTH_RESPONSE_ERROR':
            invalidate(state)
            raise ApiError(401, 'AUTH_SESSION_ENDED', '로그인 세션을 다시 시작한다.') from error
        state['ui_refresh_retry_after'] = now + 15
        raise ApiError(503, 'AUTH_REFRESH_UNAVAILABLE', '로그인 서버 연결을 확인하지 못했다. 입력을 유지한다.') from error
    return previous != result['access_token']


def screen_boundary_pending(state) -> bool:
    identity = state.get('ui_owner_id') if state.get('ui_tokens') else None
    if state.get('lr_visible_identity') != identity:
        return True
    pending = state.get('lr_operation')
    return bool(pending and state.get('lr_completed_nonce') != pending['nonce'])


def loading_screen():
    st.html('''<div role="status" aria-label="화면을 준비하는 중"
        style="max-width:420px;margin:10vh auto;padding:28px;border-radius:16px;
        border:1px solid rgba(128,128,128,.16)">
        <div style="height:20px;width:42%;border-radius:6px;background:rgba(128,128,128,.15)"></div>
        <div style="height:12px;margin-top:22px;border-radius:5px;background:rgba(128,128,128,.09)"></div>
        <div style="height:12px;width:76%;margin-top:12px;border-radius:5px;background:rgba(128,128,128,.09)"></div>
        <div style="margin-top:24px;font-size:14px;opacity:.65">잠시만 기다려 주세요.</div></div>''')


def clear_callback_query():
    # JS가 주소창을 비운 뒤 Python에 남아 있을 수 있는 callback 매개변수도 제거한다.
    for key in ('login_code', 'login_state', 'login_error'):
        if key in st.query_params:
            del st.query_params[key]


def acknowledge(state, receipt, *, broadcast=None):
    operation(state, 'ack', callback_state=receipt['state'], broadcast=broadcast)


def commit_callback(state, receipt, *, broadcast=None):
    # 완료 표시 직후 중단돼도 성공·실패에 맞는 같은 ACK를 다시 만들 수 있게 한다.
    state['lr_callback_ack'] = {'state': receipt['state'], 'broadcast': broadcast}
    state['lr_processed_callback'] = receipt['state']
    acknowledge(state, receipt, broadcast=broadcast)


def finish_logout(base_url: str, state, receipt):
    from state.ui_state import clear_account
    identifier = state.get('lr_session_id')
    try:
        result = LoginSessionClient(base_url, identifier).request('POST', '/auth/browser-sessions/logout-ticket',
            body={'redirect_uri': receipt['redirect_uri'], 'state': receipt['state']})
        ticket = result.get('ticket')
        if not isinstance(ticket, str) or not SESSION_PATTERN.fullmatch(ticket):
            raise ApiError(502, 'AUTH_RESPONSE_ERROR', '로그아웃 응답을 확인한다.')
    except ApiError:
        clear_account()
        state['lr_session_id'] = None
        state['lr_checked'] = True
        state['lr_auth_error'] = '서버 로그아웃과 브라우저 쿠키 정리를 확인하지 못했다. 다시 로그인해 확인한다.'
        operation(state, 'ack', callback_state=receipt['state'])
    else:
        clear_account()
        state['lr_session_id'] = None
        state['lr_checked'] = True
        state['lr_auth_error'] = None
        operation(state, 'logout', logout_url=base_url.rstrip('/') + '/auth/browser/logout?' + urlencode({'ticket': ticket}))
    st.rerun()


def startup(base_url: str):
    from state.ui_state import clear_account
    state = st.session_state
    try:
        # 서버 간 비밀값이 없으면 인증 화면으로 이동하기 전에 설정 오류를 알린다.
        client_secret = login_client_secret()
        if 'lr_operation' not in state:
            operation(state)
    except ApiError as error:
        st.error(error.message)
        st.stop()
    receipt = browser_auth(state.lr_operation).receipt
    job = state.get('lr_exchange_job')
    if job is not None:
        pending = state.lr_operation
        acknowledging = pending['op'] == 'ack' and pending.get('callback_state') == job.callback_state
        matching_receipt = receipt and receipt.get('nonce') == pending['nonce']
        committed = state.get('lr_processed_callback') == job.callback_state
        if committed and acknowledging and matching_receipt and (receipt.get('kind') == 'none'
                or receipt.get('kind') == 'callback' and receipt.get('state') != job.callback_state):
            # 인증 반영과 그 결과의 ACK가 끝난 작업만 버린다. 새 거래는 별도로 처리한다.
            state.pop('lr_exchange_job', None)
        elif committed and acknowledging:
            # 같은 ACK의 브라우저 확인을 기다린다. 새 nonce를 계속 만들어 재실행하지 않는다.
            loading_screen()
            st.stop()
        else:
            # 교환 중 도착한 위젯 재실행이 callback을 바꿔도 보관한 결과부터 반영한다.
            receipt = {**job.receipt, 'nonce': pending['nonce'], 'ok': True}
    if not receipt or receipt.get('nonce') != state.lr_operation['nonce']:
        loading_screen()
        st.stop()
    state['lr_completed_nonce'] = state.lr_operation['nonce']
    kind = receipt.get('kind')
    state['lr_browser_framed'] = bool(receipt.get('framed'))
    if kind in ('relay_wait', 'relay_complete', 'relay_timeout'):
        # 복귀한 새 창은 짧은 코드만 중계한다. 이 창에 로그인 상태를 만들지 않는다.
        clear_callback_query()
        if kind == 'relay_timeout':
            st.warning('원래 화면에 연결하지 못했다. 이 창을 닫고 원래 화면에서 로그인을 다시 시작한다.')
        elif kind == 'relay_wait':
            st.caption('인증 결과를 원래 화면에 전달하고 있다. 잠시 기다린다.')
        else:
            st.caption('인증 확인이 끝났다. 원래 화면에서 계속한다.')
        st.stop()
    if kind in ('gesture_required', 'logout_pending', 'popup_blocked'):
        st.caption('인증 창에서 계속한다. 창이 열리지 않으면 팝업을 허용한다.')
        st.stop()
    if kind == 'redirecting':
        loading_screen()
        st.stop()
    if kind == 'logout_ready':
        finish_logout(base_url, state, receipt)
    if kind == 'callback':
        clear_callback_query()
        # 같은 component 확인이 재도착해도 단기 코드를 두 번 교환하지 않는다.
        if state.get('lr_processed_callback') == receipt.get('state'):
            completed = state.get('lr_callback_ack') or {}
            change = completed.get('broadcast') if completed.get('state') == receipt['state'] else None
            acknowledge(state, receipt, broadcast=change)
            st.rerun()
        if receipt.get('error'):
            clear_account()
            state['lr_session_id'] = None
            state['lr_checked'] = True
            state['lr_auth_error'] = None
            commit_callback(state, receipt, broadcast='logout' if receipt['error'] == 'logged_out' else None)
            st.rerun()
        try:
            if (not all(isinstance(receipt.get(key), str) and SESSION_PATTERN.fullmatch(receipt[key])
                        for key in ('state', 'code', 'code_verifier'))
                    or receipt.get('redirect_uri') != app_public_url()):
                raise ApiError(401, 'AUTH_CALLBACK_ERROR', '로그인 확인이 만료되었다. 다시 로그인한다.')
            job = state.get('lr_exchange_job')
            if job is None or job.callback_state != receipt['state']:
                job = LoginExchange(receipt)
                state['lr_exchange_job'] = job
            # 먼저 작업을 보관한 뒤 요청한다. Streamlit 재실행이 일회용 응답을 잃게 하지 않는다.
            job.start(LoginSessionClient(base_url, client_secret=client_secret),
                {'code': receipt['code'], 'code_verifier': receipt['code_verifier'],
                 'redirect_uri': receipt['redirect_uri'], 'state': receipt['state']})
            result = job.result()
            clear_account()
            accept_access(state, result, job.completed_at)
            state['ui_active_page'] = None
            state['ui_profile_attempted'] = False
            state['ui_profile_status'] = 'pending'
            state['lr_auth_error'] = None
        except ApiError as error:
            clear_account()
            state['lr_session_id'] = None
            state['lr_checked'] = True
            state['lr_auth_error'] = error.message
            commit_callback(state, receipt)
        else:
            # 계정·토큰·프로필 상태를 모두 채운 뒤에만 처리 완료로 기록한다.
            commit_callback(state, receipt, broadcast='login' if receipt.get('reason') == 'login' else None)
        st.rerun()
    if kind == 'account_changed' and state.get('lr_signal_id') != receipt.get('signal_id'):
        if state.get('lr_session_id') and state.get('ui_tokens'):
            # 다른 탭의 알림만으로 현재 계정을 지우지 않는다. 현재 서버 세션의 종료를 확인한다.
            try:
                renew_persistent(base_url, state, verify=True)
            except ApiError as error:
                if error.status != 401:
                    # 연결 실패는 로그아웃의 증거가 아니다. 입력·화면을 유지하고 다음 확인을 기다린다.
                    st.warning(error.message)
                    return
            else:
                state['lr_signal_id'] = receipt['signal_id']
                operation(state)
                st.rerun()
        clear_account()
        state['lr_session_id'] = None
        state['lr_signal_id'] = receipt.get('signal_id')
        state['lr_checked'] = True
        state['lr_auth_error'] = None
        if receipt.get('change') == 'login' and not state.get('lr_browser_framed'):
            begin_login(base_url, state, reason='restore')
        else:
            operation(state)
        st.rerun()
    if kind == 'error' or not receipt.get('ok'):
        clear_callback_query()
        state['lr_checked'] = True
        state['lr_auth_error'] = '로그인을 확인하지 못했다. 로그인 화면에서 다시 시작한다.'
        # 실패 후에는 같은 주소로 자동 이동하지 않는다. 로그인 버튼만 재시작한다.
        return
    if not state.get('lr_checked') and not state.get('lr_browser_framed'):
        begin_login(base_url, state, reason='restore')
        st.rerun()
    state['lr_checked'] = True


def logout_persistent(base_url: str, state):
    # 브라우저에서 state를 만든 뒤 서버 폐기 → Render 쿠키 삭제 → 화면 복귀 순서로 진행한다.
    state['lr_auth_error'] = None
    operation(state, 'prepare_logout')
    st.rerun()
