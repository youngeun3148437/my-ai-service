"""대화 목록·기록·입력을 조립한다."""
import streamlit as st
from ui.common import current_api
from ui.conversations import conversation_list, open_new_dialog
from ui.chat_views import conversation_view

api = current_api()
with st.sidebar:
    conversation_list(api)
conversation = st.session_state.ui_conversation
if conversation is None:
    with st.container(key="release_empty_chat", border=True):
        st.header("어떤 이야기를 나눌까요?")
        if st.session_state.ui_conversations_status == "ready":
            st.caption("새 대화를 만들고 질문이나 파일을 보내 대화를 시작한다.")
            st.button("새 대화 만들기", icon=":material/add:", type="primary",
                      key="release_start_conversation", on_click=open_new_dialog)
        else:
            st.caption("대화 목록을 확인하고 있다. 조회 오류가 있으면 사이드바에서 다시 불러온다.")
else:
    conversation_view(api, conversation)
