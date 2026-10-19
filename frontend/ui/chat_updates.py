"""대화 안의 변경은 대화 구역에만 반영하고 메뉴·계정 프레임을 유지한다."""
import streamlit as st
from streamlit.errors import StreamlitInvalidLayoutContextError


CHAT_FRAGMENT = "release_chat_region"


def request_send(conversation_id: int):
    # 버튼 callback에서 최신 위젯 값을 고정한다. HTTP 쓰기는 대화 구역에서 한 번 실행한다.
    content = st.session_state.get(f"ui_question_{conversation_id}", "")
    if content.strip():
        st.session_state.ui_chat_send = (conversation_id, content)
    # 이름을 지정한 재실행은 callback에서만 가능하다. 2초 입력 fragment의 부모를 고른다.
    st.rerun(scope=CHAT_FRAGMENT)


def rerun_chat():
    try:
        st.rerun(scope="fragment")
    except StreamlitInvalidLayoutContextError:
        # 최초 앱 실행 중 복구한 작업은 fragment 단독 실행이 아니므로 한 번 프레임을 재조립한다.
        st.rerun()


def after_chat_write(message: str):
    st.session_state.ui_chat_notice = message
    rerun_chat()
