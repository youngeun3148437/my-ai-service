"""질문 뒤에 답변을 스트리밍하고 실패한 질문에만 복구 동작을 붙인다."""
from contextlib import closing
import streamlit as st
from clients.api_client import ApiError
from ui.chat_updates import after_chat_write as after_write
from ui.ai import apply_answer, retry_answer, validate_message


def options(*, disabled: bool = False, draft=None, conversation_id: int | None = None) -> tuple[str, bool]:
    # 기록 사용·스트리밍·자동 생성은 기본 동작이다. 사용자는 답변 표현만 고른다.
    with st.popover("", icon=":material/tune:", help="답변 방식", type="tertiary", disabled=disabled):
        style = st.selectbox("답변 방식", ["brief", "example"],
                             format_func=lambda value: "간결하게" if value == "brief" else "예시와 함께",
                             index=["brief", "example"].index(st.session_state.ui_ai_options["ui_answer_style"]),
                             key="ui_answer_style", disabled=disabled)
        st.caption("대화 기록을 이어 사용하고 답변은 실시간으로 표시한다.")
        delete_draft = False
        if draft is not None and draft.record:
            st.divider()
            # 직접 초안을 삭제한 클릭과 자동 저장·질문 전송은 composer에서 구분한다.
            delete_draft = st.button("저장된 초안 삭제", icon=":material/delete:", type="tertiary",
                                     key=f"ui_delete_draft_{conversation_id}", disabled=disabled or draft.paused)
    st.session_state.ui_ai_options["ui_answer_style"] = style
    return style, delete_draft


def answer_body(conversation_id: int, question_id: int, style: str, history: bool = True) -> dict:
    body = {"question_message_id": question_id, "answer_style": style, "use_history": True}
    identifiers = st.session_state.ui_answer_attachments.get((conversation_id, question_id), [])
    if identifiers:
        # 전송 시점에 고정한 첨부를 사용한다. 뒤의 선택 변경은 이 질문에 영향을 주지 않는다.
        body["attachment_ids"] = list(identifiers)
    return body


def run_queued_answer(api, conversation_id: int):
    # 먼저 작업을 꺼내므로 화면 재실행·오류가 AI 쓰기 요청을 자동 반복하지 않는다.
    pending = st.session_state.ui_queued_answers.pop(conversation_id, None)
    if pending is not None:
        question_id, style, history, notice = pending
        answer_notice = generate(api, conversation_id, question_id, style, history)
        after_write(" ".join(part for part in (notice, answer_notice) if part))


def generate(api, conversation_id: int, question_id: int, style: str, history: bool = True) -> str:
    body = answer_body(conversation_id, question_id, style, history)
    try:
        st.session_state.ui_partial_answers.pop(conversation_id, None)
        result = stream_answer(api, conversation_id, body)
        notice = apply_answer(api, conversation_id, result)
        return "" if notice == "답변을 저장했다." else notice
    except ApiError as error:
        # 질문 원본은 유지한다. 이 질문 아래에서 답변만 다시 요청할 수 있다.
        return "질문은 저장되었다. " + error.message


def stream_answer(api, conversation_id: int, body: dict) -> dict:
    outcome = {"result": None, "partial": ""}
    def chunks():
        # done에서 일찍 끝내거나 예외가 나도 이 HTTP 수신 연결은 닫는다.
        with closing(api.stream(f"/conversations/{conversation_id}/answers/stream", body=body)) as reader:
            for name, data in reader:
                if name == "meta":
                    continue
                if name == "delta":
                    text = data.get("text")
                    if not isinstance(text, str):
                        raise ApiError(502, "STREAM_RESPONSE_ERROR", "답변 조각을 확인할 수 없다.")
                    outcome["partial"] += text
                    st.session_state.ui_partial_answers[conversation_id] = {
                        "question_message_id": body["question_message_id"], "text": outcome["partial"]}
                    yield text
                elif name == "done":
                    validate_message(data)
                    if data["message"]["role"] != "assistant" or data["message"].get("conversation_id") != conversation_id:
                        raise ApiError(502, "STREAM_RESPONSE_ERROR", "완성 답변의 대화를 확인할 수 없다.")
                    outcome["result"] = data
                    return
                elif name == "error":
                    handle_stream_error(data)
                else:
                    raise ApiError(502, "STREAM_RESPONSE_ERROR", "답변 수신 형식을 확인할 수 없다.")
        raise ApiError(0, "STREAM_INCOMPLETE", "답변 완료를 확인하지 못했다.")
    with st.chat_message("assistant"):
        st.write_stream(chunks())
    st.session_state.ui_partial_answers.pop(conversation_id, None)
    return outcome["result"]


def handle_stream_error(data: dict):
    raise ApiError(data.get("status", 502), data.get("code", "AI_STREAM_ERROR"),
                   data.get("message", "답변 수신이 중단되었다."), log_status=data.get("log_status"),
                   attempt_id=data.get("attempt_id"), event_status=data.get("event_status"))


def pending_answer(api, conversation_id: int, question_id: int):
    # 미답변 질문에만 표시한다. 생성 대기 중에는 같은 답변의 재요청을 막는다.
    if st.session_state.ui_answer_targets.get(conversation_id) != question_id:
        return
    if conversation_id in st.session_state.ui_queued_answers:
        st.caption("답변을 준비하고 있다…")
        return
    partial = st.session_state.ui_partial_answers.get(conversation_id)
    if partial and partial["question_message_id"] == question_id:
        with st.expander("완료하지 못한 답변 보기"):
            st.write(partial["text"])
            st.caption("아직 저장된 답변으로 확인되지 않은 내용이다.")
    st.caption("답변 완료를 확인하지 못했다. 기록을 확인한 뒤 다시 요청할 수 있다.")
    with st.container(horizontal=True, horizontal_alignment="right"):
        if st.button("답변 다시 요청", icon=":material/refresh:", key=f"ui_retry_answer_{question_id}",
                     help="질문을 다시 저장하지 않고 새 AI 답변을 요청한다."):
            retry_answer(api, conversation_id, question_id, st.session_state.ui_ai_options["ui_answer_style"], True)
