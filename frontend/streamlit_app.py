"""브라우저 로그인 복원·권한 메뉴·페이지 수명을 한 진입점에서 조립한다."""
import streamlit as st
from dotenv import find_dotenv, load_dotenv
from state.ui_state import initialize, enter_conversation_page
from state.login_session_state import startup as restore_login, screen_boundary_pending
from ui.common import current_api, screen_styles, base_url
from ui.auth import ensure_profile
from ui.auth_views import authentication
from ui.compose import flush_before_navigation
from ui.admin_views import role_for_menu

load_dotenv(find_dotenv(usecwd=True))
st.set_page_config(page_title="AI 대화 서비스", layout="wide")
initialize()
screen_styles()
# 브라우저 인증·주기 확인·본문의 위치를 고정한다. 일반 실행은 기존 본문을 유지한다.
restore_frame = st.container(key="release_restore")
authentication_frame = st.container(key="release_authentication")
# 계정 경계에서만 교체 구역으로 이전 폼·계정 화면을 즉시 비운다.
clear_page = screen_boundary_pending(st.session_state)
page_frame = st.empty() if clear_page else st.container(key="release_page")
# 인증 코드의 서버 교환은 보호 페이지 등록보다 먼저 끝낸다.
with restore_frame:
    restore_login(base_url())
if st.session_state.ui_tokens is not None:
    with authentication_frame:
        authentication(base_url())
    conversation_page = st.Page("pages/02_conversations.py", title="대화", icon="💬", url_path="chat", default=True)
    dashboard_page = st.Page("pages/04_dashboard.py", title="내 사용 기록", icon="📊", url_path="usage")
    account_page = st.Page("pages/03_account.py", title="계정", icon="👤", url_path="account-settings")
    available = [conversation_page, dashboard_page, account_page]
    # 메뉴는 서버가 확인한 역할로 고른다. 관리자 API는 매 요청에 별도로 권한을 검사한다.
    if role_for_menu(current_api()) == "admin":
        available.insert(2, st.Page("pages/05_admin.py", title="서비스 운영", icon="🛠️", url_path="operations"))
else:
    available = [st.Page("pages/01_login.py", title="로그인", icon="🔑", url_path="sign-in", default=True)]

page = st.navigation(available)
with page_frame.container() if clear_page else page_frame:
    previous = st.session_state.ui_active_page
    if st.session_state.ui_tokens is not None:
        if previous == conversation_page.url_path and page.url_path != conversation_page.url_path:
            if not flush_before_navigation(current_api()):
                st.switch_page(conversation_page)
        if page.url_path != dashboard_page.url_path:
            # 개인 알림 worker는 내 사용 기록 화면에 있는 동안만 유지한다.
            realtime = st.session_state.ui_realtime_state
            if realtime is not None:
                realtime.stop()
        if not ensure_profile(current_api(), account_page=page.url_path == account_page.url_path):
            st.stop()
        if previous != page.url_path:
            if page.url_path == conversation_page.url_path:
                enter_conversation_page()
            if page.url_path == account_page.url_path:
                st.session_state.ui_profile_attempted = False
        st.session_state.ui_active_page = page.url_path

    notice = st.session_state.pop("ui_notice", None)
    notice_level = st.session_state.pop("ui_notice_level", "info")
    if notice:
        (st.warning if notice_level == "warning" else st.info)(notice)
    st.session_state.lr_visible_identity = st.session_state.ui_owner_id if st.session_state.ui_tokens else None
    page.run()
