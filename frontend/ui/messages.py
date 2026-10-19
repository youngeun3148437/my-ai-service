"""메시지 요청·응답을 말풍선과 입력 state에 연결한다."""
import streamlit as st
from clients.api_client import ApiClient
from ui.common import call
from ui.chat_updates import after_chat_write as after_write, rerun_chat


def refresh_messages(api: ApiClient, conversation_id: int, *, older: bool = False, original: bool = False) -> bool:
    # 시도 여부는 성공과 별개다. 실패해도 매 재실행마다 GET을 반복하지 않게 기록한다.
    st.session_state.ui_messages_attempted = True
    previous = st.session_state.ui_page
    # 최신 조회는 before_id 없이 요청한다. 더보기는 받은 cursor로 그 ID보다 이전 기록을 읽는다.
    params = {"before_id": previous["next_before_id"]} if older and previous else None
    if original:
        params = {"refresh": True}
    page = call(api, "GET", f"/conversations/{conversation_id}/messages", params=params)
    # 받은 page가 없으면 오류 표시만 바꾸고 기존 기록은 덮어쓰지 않는다.
    if page is None:
        st.session_state.ui_messages_error = True
        return False
    if older and previous:
        # ID를 key로 합쳐 중복을 없앤다. 같은 ID는 뒤에 온 응답이 앞의 값을 교체한다.
        records = {m["id"]: m for m in previous["messages"] + page["messages"]}
        # **page는 응답 항목을 펼친다. cursor 등은 유지하고 messages만 합친 목록으로 바꾼다.
        page = {**page, "messages": sorted(records.values(), key=lambda m: m["id"])}
    # 성공한 목록·cursor를 보관하고 조회 오류 표시를 해제한다.
    st.session_state.ui_page = page
    st.session_state.ui_messages_error = False
    return True


def send_question(api: ApiClient, conversation_id: int, content: str):
    from ui.ai import send_question as submit
    submit(api, conversation_id, content)


def valid_write_result(result: dict, action: str) -> bool:
    # 저장 응답의 형태를 검사해 성공 화면으로 넘어가도 되는지 판단한다.
    # cache_cleared=False도 저장 성공일 수 있다. 여기서는 정리 결과가 bool인지 확인한다.
    valid = type(result.get("cache_cleared")) is bool
    if action == "delete":
        valid = valid and result.get("deleted") is True
    else:
        # 저장한 메시지의 ID·역할·본문이 있어야 말풍선에 반영할 수 있다.
        message = result.get("message")
        valid = (valid and isinstance(message, dict) and type(message.get("id")) is int
                 and message.get("role") in {"user", "assistant"} and isinstance(message.get("content"), str))
    if not valid:
        st.error("저장 결과를 확인하지 못했다. 입력을 유지했다. 기록을 확인한 뒤 직접 다시 시도한다.")
    return valid


def finish_write(api: ApiClient, conversation_id: int, result: dict, *, action: str,
                 message_id: int | None = None):
    # 저장 응답을 기록에 먼저 반영하고 필요한 재조회·성공 안내를 연결한다.
    page = st.session_state.ui_page or {"messages": [], "next_before_id": None}
    # 메시지 ID를 key로 묶으면 같은 ID의 본문을 교체하거나 해당 행만 제거할 수 있다.
    rows = {m["id"]: m for m in page["messages"]}
    if action == "delete":
        rows.pop(message_id, None)
    else:
        rows[result["message"]["id"]] = result["message"]
    # **page로 cursor 등 기존 항목을 유지하고 messages만 ID 순서의 목록으로 바꾼다.
    st.session_state.ui_page = {**page, "messages": sorted(rows.values(), key=lambda m: m["id"])}
    # 반영한 기록을 표시하고 편집·삭제 확인 화면은 닫는다.
    st.session_state.ui_messages_attempted = True
    st.session_state.ui_messages_error = False
    st.session_state.ui_editing_message = None
    st.session_state.ui_deleting_message = None
    notice = "메시지를 삭제했다." if action == "delete" else ("메시지를 수정했다." if action == "edit" else "메시지를 저장했다.")
    # 캐시 정리가 확인된 경우에만 최신 기록을 GET으로 다시 읽는다.
    if result["cache_cleared"]:
        saved_page = st.session_state.ui_page
        # 저장은 이미 끝났다. 버전 검사를 거친 읽기 결과만 화면에 합친다.
        refreshed = refresh_after_versioned_write(api, conversation_id, saved_page)
        if not refreshed:
            # 저장 API는 이미 성공했다. GET 실패로 저장한 글·빈 입력을 되돌리거나 재전송하지 않는다.
            notice += " 저장 결과는 유지했다. 기록을 다시 불러온다."
    else:
        # 아직 낡은 캐시가 조회될 수 있으므로 GET 대신 확인한 저장 응답을 그대로 보여 준다.
        notice += " 최신 기록 갱신이 지연되고 있다. 저장 결과는 유지한다."
    # 안내를 다음 실행에 넘기고 재실행해 바뀐 기록·입력·버튼을 함께 표시한다.
    after_write(notice)


def merge_refreshed_messages(saved_page: dict):
    # 펼친 이전 기록은 남기고 같은 ID의 본문은 방금 조회한 값으로 바꾼다.
    received = st.session_state.ui_page
    merged = {m["id"]: m for m in saved_page["messages"] + received["messages"]}
    st.session_state.ui_page = {**received, "messages": sorted(merged.values(), key=lambda m: m["id"]),
                                "next_before_id": saved_page["next_before_id"]}


def refresh_after_versioned_write(api: ApiClient, conversation_id: int, saved_page: dict) -> bool:
    # 일반 GET의 캐시 버전 검사를 통과한 기록을 읽는다. 쓰기 API를 다시 호출하지 않는다.
    # 메시지 저장 뒤 버전 검사를 거친 기록을 GET하고 확정 응답과 병합한다.
    if not refresh_messages(api, conversation_id):
        return False
    merge_refreshed_messages(saved_page)
    return True


def prepare_messages(api: ApiClient, conversation_id: int):
    # 첫 기록 조회를 화면 공개 전에 끝낸다. 일반 fragment 실행에서는 같은 GET을 반복하지 않는다.
    if not st.session_state.ui_messages_attempted:
        refresh_messages(api, conversation_id)


def messages(api: ApiClient, conversation_id: int):
    # 선택한 대화의 기록·조회 실패 안내·이전 기록 버튼을 표시한다.
    prepare_messages(api, conversation_id)
    if st.session_state.ui_messages_error:
        if st.button("다시 불러오기", key="ui_retry_messages"):
            if refresh_messages(api, conversation_id, original=True):
                rerun_chat()
    page = st.session_state.ui_page
    # 아직 받은 기록이 없는 상태와 조회에 성공한 빈 목록을 구분한다.
    if page is not None and not page["messages"]:
        st.caption("첫 질문을 입력한다.")
    if page and page["messages"]:
        latest = page["messages"][-1]
        if latest["role"] == "user" and conversation_id not in st.session_state.ui_answer_targets:
            st.session_state.ui_answer_targets[conversation_id] = latest["id"]
        elif latest["role"] == "assistant" and conversation_id not in st.session_state.ui_queued_answers:
            st.session_state.ui_answer_targets.pop(conversation_id, None)
    # 기록을 고정 높이의 작은 스크롤 상자에 가두지 않고 본문 너비로 이어 표시한다.
    history_container = st.container(key="release_message_history", border=False)
    with history_container:
        if page is None:
            st.info("기록을 불러오지 못했다. 다시 불러오거나 질문을 입력할 수 있다.")
            # 조회 실패에도 기록 영역은 반환한다. 직접 요청한 답변을 뒤늦게 실행하지 않는다.
            return history_container
        # cursor가 있으면 그 ID보다 이전 기록을 이어 읽을 수 있다.
        if page["next_before_id"] is not None:
            if st.button("이전 기록 더 보기", key="ui_older_messages"):
                from clients.chat_scroll import preserve_history
                preserve_history(conversation_id)
                if refresh_messages(api, conversation_id, older=True):
                    rerun_chat()
        for message in page["messages"]:
            message_item(api, conversation_id, message)
    from ui.feedback_views import feedback_dialog_if_open
    feedback_dialog_if_open(api)
    # 본문 입력을 만든 뒤에도 이 영역 안에 생성 중인 답변을 이어 그릴 수 있다.
    return history_container


def message_input(api: ApiClient, conversation_id: int):
    from ui.compose import composer
    composer(api, conversation_id)


def start_edit(message_id: int, content: str):
    # 선택한 메시지의 기존 본문으로 편집창만 연다. 이 시점에는 PATCH하지 않는다.
    st.session_state.ui_editing_message = message_id
    st.session_state.ui_deleting_message = None
    st.session_state[f"ui_edit_{message_id}"] = content
    st.session_state.ui_edit_texts[message_id] = content


def start_delete(message_id: int):
    # 선택한 메시지의 삭제 확인만 연다. 아직 DELETE를 보내지 않는다.
    st.session_state.ui_deleting_message = message_id
    st.session_state.ui_editing_message = None


def cancel_message_action():
    # 요청 없이 편집·삭제 확인을 닫고 원래 기록을 유지한다.
    st.session_state.ui_editing_message = None
    st.session_state.ui_deleting_message = None


def message_item(api: ApiClient, conversation_id: int, message: dict):
    # 한 메시지의 말풍선과 선택한 메시지만의 편집·삭제 확인 화면을 만든다.
    message_id = message["id"]
    with st.container(key=f"release_message_row_{message_id}", border=False), st.chat_message(message["role"]):
        st.write(message["content"])
        if message["role"] == "user":
            from ui.ai_views import pending_answer
            pending_answer(api, conversation_id, message_id)
        # 아이콘의 callback은 대상 ID와 화면 상태만 바꾼다. 실제 요청은 아래 확인 버튼에서 한다.
        if st.session_state.ui_editing_message != message_id:
            with st.container(key=f"release_message_actions_{message_id}", horizontal=True, horizontal_alignment="right"):
                if message["role"] == "assistant":
                    from ui.feedback_views import feedback
                    feedback(api, message_id)
                st.button("", icon=":material/edit:", key=f"ui_edit_open_{message_id}", help="메시지 수정", width="content", type="tertiary",
                          on_click=start_edit, args=(message_id, message["content"]))
                st.button("", icon=":material/delete:", key=f"ui_delete_open_{message_id}", help="메시지 삭제", width="content", type="tertiary",
                          on_click=start_delete, args=(message_id,))
        if st.session_state.ui_editing_message == message_id:
            edit_key = f"ui_edit_{message_id}"
            if edit_key not in st.session_state:
                st.session_state[edit_key] = st.session_state.ui_edit_texts.get(message_id, message["content"])
            # 수정 폼은 제출할 때 최신 본문을 함께 전달한다. 취소는 서버 기록을 바꾸지 않는다.
            with st.form(f"edit_{message_id}"):
                content = st.text_area("수정할 내용", max_chars=5000, key=edit_key)
                with st.container(horizontal=True, horizontal_alignment="right"):
                    submitted = st.form_submit_button("수정 저장", type="primary")
                    st.form_submit_button("취소", on_click=cancel_message_action)
            if submitted:
                if content.strip():
                    update_message(api, conversation_id, message_id, content.strip())
                else:
                    st.warning("내용을 입력한다.")
        if st.session_state.ui_deleting_message == message_id:
            st.warning("이 메시지를 삭제한다. 삭제한 메시지는 되돌릴 수 없다.")
            with st.container(key=f"ui_message_delete_actions_{message_id}", horizontal=True, horizontal_alignment="right"):
                st.button("취소", key=f"ui_cancel_delete_{message_id}", on_click=cancel_message_action, width="content")
                # 삭제를 확인한 시점에만 해당 메시지 ID로 DELETE를 요청한다.
                if st.button("삭제", key=f"ui_delete_{message_id}", type="primary", width="content"):
                    delete_message(api, conversation_id, message_id)


def update_message(api: ApiClient, conversation_id: int, message_id: int, content: str):
    result = call(api, "PATCH", f"/messages/{message_id}", body={"content": content})
    # 실패·형식 오류에는 편집 입력을 남기고, 확인한 저장 응답만 기록에 반영한다.
    if result is None or not valid_write_result(result, "edit"):
        return
    finish_write(api, conversation_id, result, action="edit", message_id=message_id)


def delete_message(api: ApiClient, conversation_id: int, message_id: int):
    # 삭제 대상은 메시지 ID로 지정한다. 요청 본문은 보내지 않는다.
    result = call(api, "DELETE", f"/messages/{message_id}")
    # 실패이면 화면의 기존 행을 남긴다. deleted=True를 확인한 뒤에만 제거한다.
    if result is None or not valid_write_result(result, "delete"):
        return
    finish_write(api, conversation_id, result, action="delete", message_id=message_id)
