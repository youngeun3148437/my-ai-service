"""사용자별 로그인·선택·목록·로컬 입력을 화면 세션에 보관한다."""
import streamlit as st


def initialize():
    defaults = {"ui_tokens": None, "ui_owner_id": None, "ui_conversations": [], "ui_conversation_cursor": None,
                "ui_conversation": None, "ui_page": None, "ui_conversations_attempted": False,
                "ui_conversations_status": "pending", "ui_messages_attempted": False, "ui_messages_error": False,
                "ui_notice": None, "ui_last_error": None, "ui_active_page": None, "ui_question_texts": {},
                "ui_editing_message": None, "ui_deleting_message": None, "ui_edit_texts": {},
                "ui_profile_attempted": False, "ui_profile": None, "ui_profile_missing": False,
                "ui_profile_status": "pending", "ui_profile_error": "", "ui_profile_name": "",
                "ui_auth_mode": "login", "ui_signup_done": False, "ui_signup_confirmation": False,
                "ui_new_conversation_open": False, "ui_new_title": "", "ui_deleting_conversation": None,
                "ui_renaming_conversation": None, "ui_rename_title_texts": {}}
    defaults.update({"ui_token_deadline": 0.0, "ui_refresh_retry_after": 0.0,
                     "ui_draft_states": {}, "ui_draft_ids": {}, "ui_pending_messages": {},
                     "ui_partial_answers": {}, "ui_answer_targets": {}, "ui_queued_answers": {},
                     "ui_attachment_states": {}, "ui_pending_message_attachments": {},
                     "ui_answer_attachments": {}, "ui_feedback_states": {},
                     "ui_dashboard_state": None, "ui_realtime_state": None,
                     "ui_ai_options": {"ui_answer_style": "brief"}})
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    # 다른 페이지에서 위젯이 없어져도 서버가 받은 입력은 대화별로 보관한다.
    for conversation_id in st.session_state.ui_question_texts:
        key = f"ui_question_{conversation_id}"
        if key in st.session_state:
            st.session_state.ui_question_texts[conversation_id] = st.session_state[key]
    editing = st.session_state.ui_editing_message
    if editing is not None and f"ui_edit_{editing}" in st.session_state:
        st.session_state.ui_edit_texts[editing] = st.session_state[f"ui_edit_{editing}"]
    if "ui_title" in st.session_state:
        st.session_state.ui_new_title = st.session_state.ui_title
    if "ui_name" in st.session_state:
        st.session_state.ui_profile_name = st.session_state.ui_name
    renaming = st.session_state.ui_renaming_conversation
    if renaming and f"ui_rename_title_{renaming['id']}" in st.session_state:
        st.session_state.ui_rename_title_texts[renaming["id"]] = st.session_state[f"ui_rename_title_{renaming['id']}"]
    # 생성한 위젯의 값은 다음 실행에서, 위젯을 만들기 전에 바꾼다.
    pending = st.session_state.pop("ui_pending_question", None)
    if pending:
        conversation_id, text = pending
        st.session_state[f"ui_question_{conversation_id}"] = text
        st.session_state.ui_question_texts[conversation_id] = text
    if st.session_state.pop("ui_clear_title", False):
        st.session_state.ui_title = ""
        st.session_state.ui_new_title = ""
    if st.session_state.pop("ui_clear_signup_passwords", False):
        st.session_state.ui_signup_password = ""
        st.session_state.ui_signup_password_confirm = ""


def clear_account():
    # 명시적 로그아웃·다른 사용자 로그인에서는 이전 계정의 값 전체를 비운다.
    realtime = st.session_state.get("ui_realtime_state")
    if realtime is not None:
        realtime.stop()
    for key in list(st.session_state):
        if key.startswith("ui_"):
            del st.session_state[key]
    initialize()


def select_conversation(conversation: dict):
    st.session_state.ui_conversation = conversation
    st.session_state.ui_page = None
    st.session_state.ui_messages_attempted = False
    st.session_state.ui_messages_error = False
    st.session_state.ui_editing_message = None
    st.session_state.ui_deleting_message = None


def discard_conversation(conversation_id: int):
    selected = st.session_state.ui_conversation
    if selected and selected["id"] == conversation_id:
        st.session_state.ui_conversation = None
        st.session_state.ui_page = None
        st.session_state.ui_messages_attempted = False
        st.session_state.ui_messages_error = False
        st.session_state.ui_editing_message = None
        st.session_state.ui_deleting_message = None
    st.session_state.ui_conversations = [c for c in st.session_state.ui_conversations if c["id"] != conversation_id]
    for key in ("ui_draft_states", "ui_draft_ids", "ui_pending_messages", "ui_partial_answers",
                "ui_answer_targets", "ui_queued_answers", "ui_attachment_states", "ui_pending_message_attachments"):
        st.session_state[key].pop(conversation_id, None)
    st.session_state.ui_question_texts.pop(conversation_id, None)
    st.session_state.ui_rename_title_texts.pop(conversation_id, None)
    st.session_state.pop(f"ui_question_{conversation_id}", None)
    st.session_state.ui_deleting_conversation = None
    st.session_state.ui_renaming_conversation = None


def enter_conversation_page():
    st.session_state.ui_conversations_attempted = False
    st.session_state.ui_messages_attempted = False


def after_write(message: str):
    st.session_state.ui_notice = message
    st.rerun()


def merge_message_page(page: dict, *, older: bool = False):
    previous = st.session_state.ui_page
    if older and previous:
        # 페이지 경계의 같은 ID를 한 번만 표시한다. 화면의 목록이며 DB 캐시를 대체하지 않는다.
        records = {m["id"]: m for m in previous["messages"] + page["messages"]}
        # **page는 응답 항목을 펼친다. messages만 합친 목록으로 교체하고 새 cursor는 유지한다.
        page = {**page, "messages": sorted(records.values(), key=lambda m: m["id"])}
    st.session_state.ui_page = page
