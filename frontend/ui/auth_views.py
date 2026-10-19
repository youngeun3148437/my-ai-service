"""계정 화면은 ui.auth, 주기 갱신은 이 모듈이 맡는다."""
from time import monotonic
import streamlit as st
from clients.api_client import ApiError
from state.auth_state import renew_tokens
from state.login_session_state import SESSION_CHECK_SECONDS

@st.fragment(run_every=30)
def authentication(base_url: str):
    # 화면 진입은 최근 확인을 공유한다. 주기 확인은 별도 시각으로 실행해 일반 요청이 미루지 못한다.
    now = monotonic()
    verify = now >= st.session_state.get('lr_periodic_check_due', now + SESSION_CHECK_SECONDS)
    try:
        renew_tokens(base_url, st.session_state, verify=verify, clock=lambda: now)
        if verify:
            st.session_state.lr_periodic_check_due = now + SESSION_CHECK_SECONDS
    except ApiError as error:
        if error.code == 'AUTH_SESSION_ENDED':
            st.session_state.ui_notice = error.message
            st.rerun()
        st.warning(error.message)
