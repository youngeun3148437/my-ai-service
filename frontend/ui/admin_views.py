"""신뢰한 역할로 관리자 메뉴를 구성하고 서비스 전체 읽기 화면을 표시한다."""
from time import monotonic
import streamlit as st
from clients.api_client import ApiError
from state.admin_state import AdminDashboardController, clear_admin
from state.dashboard_state import DashboardState, query_filters
from ui.dashboard_views import filter_form, render_results, request_error, sync_caption


def role_for_menu(api) -> str:
    now = monotonic()
    if now < st.session_state.get("ui_role_until", 0):
        return st.session_state.get("ui_role", "user")
    try:
        result = api.request("GET", "/me/role")
        role = "admin" if result.get("role") == "admin" else "user"
    except ApiError as error:
        request_error(error)
        role = "user"
    st.session_state.ui_role = role
    st.session_state.ui_role_until = now + 30
    if role != "admin":
        st.session_state.pop("ui_admin_dashboard_state", None)
    return role


@st.fragment(run_every=30)
def admin_dashboard(api):
    # 관리자 신호를 개인 Pub/Sub 채널에 섞지 않는다. 권한 검사된 집계를 주기 조회한다.
    if role_for_menu(api) != "admin":
        st.warning("서비스 운영 화면에 접근할 수 없다.")
        return
    if st.session_state.get("ui_admin_dashboard_state") is None:
        st.session_state.ui_admin_dashboard_state = DashboardState()
    state = st.session_state.ui_admin_dashboard_state
    controller = AdminDashboardController(api, state)
    st.header("서비스 운영")
    st.caption("전체 서비스 · 모든 사용자의 AI 요청과 운영 지표를 확인한다.")
    submitted = filter_form(state, "operations", admin=True)
    try:
        if submitted or state.active_filters is None:
            controller.search(query_filters(state.start, state.end, state.mode, state.status))
            st.session_state.ui_admin_last_sync = monotonic()
        elif monotonic() - st.session_state.get("ui_admin_last_sync", 0) >= 30:
            controller.recover()
            st.session_state.ui_admin_last_sync = monotonic()
    except ApiError as error:
        if error.status in (401, 403):
            clear_admin(state)
            st.session_state.ui_role = "user"
            st.session_state.ui_role_until = 0
        else:
            state.error = error.message
        request_error(error)
    if state.error:
        st.error(state.error)
        if st.button("다시 불러오기", icon=":material/refresh:", key="ui_admin_retry"):
            st.session_state.ui_admin_last_sync = 0
            st.rerun()
    if state.aggregate is not None:
        sync_caption(state, admin=True, delayed=bool(state.error))
        render_results(state, controller, admin=True)
