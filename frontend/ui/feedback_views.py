"""답변 우하단의 평가 아이콘에서 입력과 원본 평가 기록을 연다."""

from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from clients.api_client import ApiError
from state.feedback_state import FeedbackController, FeedbackState

RATINGS = {1: "1 · 매우 아쉬움", 2: "2 · 아쉬움", 3: "3 · 보통", 4: "4 · 도움됨", 5: "5 · 매우 도움됨"}


def close_feedback():
    st.session_state.pop("ui_feedback_answer", None)


def open_feedback(answer_id: int):
    # 아이콘은 창만 연다. 점수·의견·미확정 UUID는 답변별 독립 상태에 남는다.
    st.session_state.ui_feedback_answer = answer_id


def feedback(api, answer_id: int):
    st.button("", icon=":material/rate_review:", help="답변 평가", type="tertiary",
              key=f"ui_feedback_open_{answer_id}", on_click=open_feedback, args=(answer_id,))


def feedback_dialog_if_open(api):
    answer_id = st.session_state.get("ui_feedback_answer")
    if answer_id is not None:
        feedback_dialog(api, answer_id)


@st.dialog("답변 평가", on_dismiss=close_feedback)
def feedback_dialog(api, answer_id: int):
    states = st.session_state.ui_feedback_states
    if answer_id not in states:
        states[answer_id] = FeedbackState()
    state = states[answer_id]
    controller = FeedbackController(api, answer_id, state)
    # 창을 열었을 때만 원본 평가를 읽는다. 대화의 모든 답변에 조회 요청을 보내지 않는다.
    if state.records is None and not state.error:
        try:
            controller.load()
        except ApiError as error:
            state.error = f"{error.code}: {error.message}"
    with st.container():
        rating_key, comment_key = f"ui_rating_{answer_id}", f"ui_feedback_comment_{answer_id}"
        # 페이지 왕복에서도 제출한 입력과 미확정 요청은 별도 상태에 남는다.
        if state.pending or state.reset_form or rating_key not in st.session_state:
            st.session_state[rating_key] = state.pending["rating"] if state.pending else state.rating
        if state.pending or state.reset_form or comment_key not in st.session_state:
            st.session_state[comment_key] = state.pending["comment"] if state.pending else state.comment
        state.reset_form = False
        with st.form(f"feedback_{answer_id}"):
            rating = st.selectbox("답변 점수", list(RATINGS), format_func=RATINGS.get,
                                  key=rating_key, disabled=bool(state.pending))
            comment = st.text_area("평가 의견", max_chars=1000, key=comment_key, disabled=bool(state.pending),
                                   placeholder="도움이 된 점이나 아쉬운 점을 적는다.")
            with st.container(key="release_feedback_actions", horizontal=True, horizontal_alignment="right"):
                # 키보드 제출도 저장을 실행하도록 먼저 만든다. CSS는 닫기·저장 순서로 배치한다.
                submitted = st.form_submit_button("같은 평가 재시도" if state.pending else "평가 저장", type="primary")
                dismissed = st.form_submit_button("닫기")
        if dismissed:
            state.rating, state.comment = rating, comment
            close_feedback()
            st.rerun()
        if submitted:
            try:
                controller.save(rating, comment)
            except ApiError as error:
                state.error = f"{error.code}: {error.message}"
                if error.code == "AUTH_SESSION_ENDED":
                    st.session_state.ui_notice = error.message
            st.rerun()
        if state.pending:
            st.warning("평가 저장 결과를 확인한다. 같은 평가 재시도는 보존한 번호·점수·의견을 사용한다.")
            checked = st.checkbox("기록 확인 후 이전 평가 종료", key=f"ui_feedback_finish_confirm_{answer_id}")
            if st.button("이전 평가 종료", disabled=not checked, key=f"ui_feedback_finish_{answer_id}"):
                controller.finish_pending()
                st.rerun()
        st.divider()
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown("**내 평가 기록**")
            reload_records = st.button("", icon=":material/refresh:", help="평가 기록 다시 불러오기",
                                       type="tertiary", key=f"ui_feedback_list_{answer_id}")
        if reload_records:
            try:
                controller.load()
            except ApiError as error:
                state.error = f"{error.code}: {error.message}"
                if error.status in (401, 403):
                    state.records = None
            st.rerun()
        if state.notice:
            st.success(state.notice)
            state.notice = ""
        if state.error:
            st.error(state.error)
        if state.records == []:
            st.caption("저장된 평가가 없다.")
        for record in state.records or []:
            st.write(f"{RATINGS[record['rating']]} · {record['comment']}")
            stamp = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
            st.caption(f"{stamp.astimezone(ZoneInfo('Asia/Seoul')):%Y-%m-%d %H:%M}")
        if state.next_before_id is not None:
            if st.button("이전 평가 더 보기", key=f"ui_feedback_more_{answer_id}"):
                try:
                    controller.load(older=True)
                except ApiError as error:
                    state.error = f"{error.code}: {error.message}"
                st.rerun()
