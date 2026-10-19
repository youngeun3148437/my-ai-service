"""기록·입력·AI 조각을 한 대화 fragment에 모아 사이드바 재실행을 피한다."""
import streamlit as st

from clients.chat_scroll import chat_scroll, prepare_chat_layout
from ui.ai import send_question
from ui.ai_views import run_queued_answer
from ui.chat_updates import CHAT_FRAGMENT
from ui.compose import prepare_composer
from ui.messages import messages, message_input, prepare_messages


@st.fragment(key=CHAT_FRAGMENT)
def conversation_view(api, conversation: dict):
    identifier = conversation["id"]
    # 느린 첫 초안·첨부 조회를 기록 표시 전에 끝낸다. 표시 뒤 늦게 높이가 늘어나는 일을 줄인다.
    prepare_composer(api, identifier)
    prepare_messages(api, identifier)
    send = st.session_state.pop("ui_chat_send", None)
    if send is not None and send[0] == identifier:
        send_question(api, identifier, send[1], rerun=False)
    prepare_chat_layout(identifier)
    with st.container(key=f"release_chat_surface_{identifier}", border=False):
        st.header(conversation["title"])
        notice = st.session_state.pop("ui_chat_notice", None)
        if notice:
            st.info(notice)
        history = messages(api, identifier)
        message_input(api, identifier)
        chat_scroll(identifier)
        # 질문이 기록에 먼저 나타나고 동일한 기록 영역에서 답변 조각을 이어 받는다.
        if history is not None:
            with history:
                run_queued_answer(api, identifier)
