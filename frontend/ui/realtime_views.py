"""개인 로그 화면에 들어오면 구독하고 변경 신호 뒤 원본을 다시 읽는다."""
from time import monotonic
import streamlit as st
from clients.api_client import ApiError
from clients.realtime_client import RealtimeConfig
from state.auth_state import renew_tokens
from state.dashboard_state import DashboardController
from state.realtime_state import RealtimeState


def realtime_controls(api):
    if st.session_state.ui_realtime_state is None:
        st.session_state.ui_realtime_state = RealtimeState()
    state = st.session_state.ui_realtime_state
    dashboard = st.session_state.ui_dashboard_state
    tokens = st.session_state.ui_tokens
    if tokens is None or dashboard is None or dashboard.active_filters is None:
        state.stop()
        return
    if not state.enabled:
        state.enabled = True
        state.last_sync = monotonic()
    try:
        state.consume(str(tokens["user_id"]), tokens["access_token"])
        renew_tokens(api.base_url, st.session_state, force=state.auth_expired)
        state.auth_expired = False
        tokens = st.session_state.ui_tokens
        state.consume(str(tokens["user_id"]), tokens["access_token"])
        state.ensure(RealtimeConfig(api.base_url, str(tokens["user_id"]), tokens["access_token"]),
                     source=getattr(api, "event_source", None))
        if state.needs_refresh(monotonic()):
            DashboardController(api, dashboard).recover()
            state.synchronized(monotonic())
    except ApiError as error:
        state.error = error.message
        state.retry_after = monotonic() + 5
        if error.code == "AUTH_SESSION_ENDED":
            state.stop()
            dashboard.clear_results()
            st.session_state.ui_notice = error.message
            st.rerun()
        if dashboard.aggregate is None:
            # 접근이 거절된 기록과 그 알림 연결을 함께 비운다. 메뉴 전체를 재실행할 필요는 없다.
            state.stop()
    last = f"마지막 갱신 {dashboard.last_synced_at:%H:%M:%S}" if dashboard.last_synced_at is not None else "첫 조회 대기"
    if state.error or dashboard.error:
        status = "갱신 지연 · 이전 기록 표시"
    elif state.blocked:
        status = "알림 연결 중단 · 30초마다 확인"
    else:
        status = "자동 갱신 중" if state.connected else "알림 연결 중 · 30초마다 확인"
    with st.container(horizontal=True, vertical_alignment="center"):
        st.caption(f"{status} · {last}")
        if state.blocked or state.error:
            st.button("다시 연결", icon=":material/refresh:", key="ui_realtime_reconnect", type="tertiary",
                      on_click=reconnect, args=(state,))


def reconnect(state):
    # 다음 fragment 실행 전에 연결·재조회 조건만 바꾼다. 메뉴와 페이지는 유지한다.
    state.stop()
    state.enabled, state.error, state.dirty, state.retry_after = True, "", True, 0.0
