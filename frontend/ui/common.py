"""API 주소·사용자별 클라이언트·공통 요청 안내를 연결한다."""
import os
import streamlit as st
from clients.api_client import ApiClient, ApiError
from config.api_config import ApiConfigurationError, resolve_api_base_url
from streamlit.errors import StreamlitSecretNotFoundError
from state.auth_state import AuthenticatedClient


def base_url() -> str:
    def read_secret():
        try:
            # Cloud의 Secrets와 로컬 작업 위치의 .streamlit/secrets.toml을 읽는다.
            return st.secrets.get("API_BASE_URL")
        except StreamlitSecretNotFoundError as error:
            if error.error_id == "no-secrets-found":
                return None
            # TOML 문법 오류를 로컬 주소로 덮지 않는다. 전체 설정 원문도 표시하지 않는다.
            raise ApiConfigurationError("Secrets의 TOML 문법과 파일 위치를 확인한다.") from None

    try:
        return resolve_api_base_url(os.environ, read_secret)
    except ApiConfigurationError as error:
        st.error(str(error))
        st.info("로컬은 .env, Community Cloud는 Settings → Secrets에서 API_BASE_URL을 확인한다.")
        st.stop()


def screen_styles():
    # 고정 CSS는 계정 카드와 대화 목록을 꾸민다. 사용자 제목·입력은 Streamlit이 그대로 표시한다.
    st.html("""<style>
    .st-key-ui_auth_card {
        width: 100%; max-width: 460px; margin-left: auto; margin-right: auto;
        padding: 20px; border: 1px solid rgba(128,128,128,.22); border-radius: 12px;
    }
    .st-key-ui_auth_card button[kind^="primary"],
    [data-testid="stForm"] button[kind^="primary"] {
        background: #2866aa; border-color: #2866aa; color: white;
    }
    .st-key-ui_auth_card button[kind^="primary"]:hover,
    [data-testid="stForm"] button[kind^="primary"]:hover {
        background: #1c528e; border-color: #1c528e; color: white;
    }
    .st-key-ui_auth_card button[kind="tertiary"] { color: #2866aa; }
    .st-key-ui_auth_card button[kind="tertiary"]:hover { text-decoration: underline; }
    .st-key-ui_profile_actions, .st-key-ui_profile_logout_actions,
    .st-key-ui_conversation_delete_actions, [class*="st-key-ui_message_delete_actions_"] {
        flex-wrap: nowrap; min-width: 0;
    }
    .st-key-ui_profile_actions { flex-direction: row-reverse; justify-content: space-between; }
    .st-key-ui_profile_actions [data-testid="stElementContainer"],
    .st-key-ui_profile_logout_actions [data-testid="stElementContainer"],
    .st-key-ui_conversation_delete_actions [data-testid="stElementContainer"],
    [class*="st-key-ui_message_delete_actions_"] [data-testid="stElementContainer"] {
        flex: 0 0 auto !important; width: auto !important; min-width: 0 !important;
    }
    [class*="st-key-ui_conversation_row_"] button,
    [class*="st-key-ui_conversation_active_"] button {
        border: none; background: transparent; color: inherit;
        justify-content: flex-start; box-shadow: none; text-align: left;
    }
    [class*="st-key-ui_conversation_row_"] button p,
    [class*="st-key-ui_conversation_active_"] button p { text-align: left; }
    [class*="st-key-ui_conversation_row_"] button > div,
    [class*="st-key-ui_conversation_active_"] button > div { justify-content: flex-start; }
    [class*="st-key-ui_conversation_row_"] [data-testid="stHorizontalBlock"],
    [class*="st-key-ui_conversation_active_"] [data-testid="stHorizontalBlock"] {
        flex-wrap: nowrap; gap: 4px;
    }
    [class*="st-key-ui_conversation_row_"] [data-testid="stColumn"]:first-child,
    [class*="st-key-ui_conversation_active_"] [data-testid="stColumn"]:first-child {
        flex: 1 1 0; min-width: 0;
    }
    [class*="st-key-ui_conversation_row_"] [data-testid="stColumn"]:not(:first-child),
    [class*="st-key-ui_conversation_active_"] [data-testid="stColumn"]:not(:first-child) {
        flex: 0 0 40px; width: 40px; min-width: 40px;
    }
    [class*="st-key-ui_conversation_row_"] [data-testid="stColumn"]:first-child button > div,
    [class*="st-key-ui_conversation_active_"] [data-testid="stColumn"]:first-child button > div,
    [class*="st-key-ui_conversation_row_"] [data-testid="stColumn"]:first-child button span,
    [class*="st-key-ui_conversation_active_"] [data-testid="stColumn"]:first-child button span {
        min-width: 0; width: 100%; justify-content: flex-start;
    }
    [class*="st-key-ui_conversation_row_"] [data-testid="stColumn"]:first-child button p,
    [class*="st-key-ui_conversation_active_"] [data-testid="stColumn"]:first-child button p {
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    [class*="st-key-ui_conversation_active_"] button { background: rgba(128,128,128,.14); }
    [class*="st-key-ui_conversation_row_"] button:hover { background: rgba(128,128,128,.07); }
    [data-testid="stSidebar"] .st-key-release_conversation_list {
        max-height: max(150px, calc(100dvh - 320px)); overflow-y: auto;
        overscroll-behavior: contain; scrollbar-gutter: stable; padding-right: 4px; gap: 4px;
    }
    .st-key-release_composer {
        border-radius: 20px; padding: 12px 16px;
        box-shadow: 0 3px 18px rgba(28,52,82,.04);
    }
    button[kind="primary"] { background: #2866aa; border-color: #2866aa; color: white; }
    button[kind="primary"]:hover { background: #1c528e; border-color: #1c528e; color: white; }
    .st-key-release_composer [data-baseweb="textarea"] {
        border: none; background: transparent; box-shadow: none;
    }
    .st-key-release_composer_tools { gap: 8px; align-items: center; }
    .st-key-release_composer_tools button[kind="secondary"],
    [class*="st-key-release_message_actions_"] button {
        border-color: transparent; background: transparent; box-shadow: none;
    }
    .st-key-release_composer_tools button[kind="secondary"]:hover,
    [class*="st-key-release_message_actions_"] button:hover {
        background: rgba(128,128,128,.09);
    }
    [class*="st-key-release_message_actions_"] {
        gap: 2px; margin-top: 2px;
    }
    .st-key-release_feedback_actions { flex-direction: row-reverse; justify-content: flex-start; }
    [class*="st-key-release_attachment_chips_"] button {
        border-radius: 18px; font-size: .85rem; padding: 3px 10px;
    }
    .st-key-release_account_card {
        max-width: 680px; padding: 24px; border-radius: 18px;
    }
    .release-avatar {
        width: 64px; height: 64px; border-radius: 50%; display: flex;
        align-items: center; justify-content: center; background: rgba(40,102,170,.12);
        color: #2866aa; font-size: 28px; font-weight: 600;
    }
    .st-key-release_empty_chat {
        max-width: 780px; margin: 7vh auto 0; padding: 32px; border-radius: 20px;
    }
    [data-testid="stMetric"] {
        padding: 14px 16px; border: 1px solid rgba(128,128,128,.16); border-radius: 14px;
    }
    @media (max-width: 640px) {
        .st-key-release_account_card, .st-key-release_empty_chat { padding: 18px; }
        .release-avatar { width: 44px; height: 44px; font-size: 22px; }
        .st-key-release_composer { padding: 10px; }
    }
    </style>""")


def current_api():
    if st.session_state.ui_tokens is None:
        st.info("로그인한 뒤 대화를 사용할 수 있다.")
        st.stop()
    return AuthenticatedClient(base_url(), st.session_state)


def call(api: ApiClient, method: str, path: str, **kwargs) -> dict | None:
    st.session_state.ui_last_error = None
    try:
        with st.spinner("처리하고 있다…"):
            return api.request(method, path, **kwargs)
    except ApiError as error:
        st.session_state.ui_last_error = error
        if error.code == "AUTH_SESSION_ENDED":
            st.session_state.ui_notice = error.message
            st.rerun()
        st.error(error.message)
        if method.upper() != "GET" and (error.status == 0 or error.status >= 500 or error.code == "RESPONSE_ERROR"):
            st.warning("서버에 반영되었을 수 있다. 기록을 확인한 뒤 직접 다음 동작을 선택한다. 요청은 자동 반복하지 않는다.")
        return None
