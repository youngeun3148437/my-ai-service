"""로그인 뒤 대화를 불러오고, 생성·선택을 즉시 대화 화면에 반영한다."""
import streamlit as st
from clients.api_client import ApiClient
from ui.common import call
from state.ui_state import after_write, discard_conversation, select_conversation
from ui.compose import flush_before_navigation


def load_conversations(api: ApiClient, *, older: bool = False) -> bool:
    # * 뒤의 older는 older=True처럼 이름으로 전달한다. 반환 bool은 조회 성공 여부다.
    # attempted는 조회를 시도했다는 표시다. 성공 여부는 아래의 status에 따로 기록한다.
    st.session_state.ui_conversations_attempted = True
    # 기본 조회는 최신 목록을 읽고, older=True는 cursor보다 이전 대화를 요청한다.
    cursor = st.session_state.ui_conversation_cursor
    params = {"before_id": cursor} if older else None
    # 더 보기이면 기존 목록에 합친다. 기본 조회이면 성공한 새 목록으로 교체한다.
    existing = st.session_state.ui_conversations if older else []
    page = call(api, "GET", "/me/conversations", params=params)
    if page is None:
        st.session_state.ui_conversations_status = "error"
        return False  # 실패한 조회로 이미 보이는 목록을 지우지 않는다.
    # 성공 응답을 목록·다음 페이지 기준값·준비 완료 상태에 함께 반영한다.
    st.session_state.ui_conversations = merge_conversations(existing, page["conversations"])
    st.session_state.ui_conversation_cursor = page["next_before_id"]
    st.session_state.ui_conversations_status = "ready"
    return True


def open_conversation(api: ApiClient, conversation: dict) -> bool:
    from ui.messages import refresh_messages
    if not flush_before_navigation(api):
        return False
    selected = st.session_state.ui_conversation
    # 다른 대화로 이동할 때는 선택을 바꾸고 기록·편집 상태를 초기화한다.
    if selected is None or selected["id"] != conversation["id"]:
        select_conversation(conversation)
    # 같은 대화를 다시 선택해도 GET은 실행해 해당 ID의 기록을 갱신한다.
    refresh_messages(api, conversation["id"])
    return True




def open_new_dialog():
    # 다른 관리 창을 닫고 새 대화 창을 연다. 기존에 입력한 제목은 이어 쓴다.
    st.session_state.ui_deleting_conversation = None
    st.session_state.ui_renaming_conversation = None
    st.session_state.ui_new_conversation_open = True
    st.session_state.ui_title = st.session_state.ui_new_title


def close_new_dialog():
    # 새 대화 창의 표시 상태를 끈다.
    st.session_state.ui_new_conversation_open = False


@st.dialog("새 대화", on_dismiss=close_new_dialog)
def new_conversation_dialog(api: ApiClient):
    # 제목 입력·취소를 처리하고 대화 만들기를 제출한 시점에만 POST로 이어 간다.
    with st.form("new_conversation"):
        title = st.text_input("새 대화 제목", max_chars=100, key="ui_title")
        with st.container(horizontal=True, horizontal_alignment="right"):
            created = st.form_submit_button("대화 만들기", type="primary")
            cancelled = st.form_submit_button("취소")
    if cancelled:
        close_new_dialog()
        st.rerun()
    if created:
        st.session_state.ui_new_title = title
        if not title.strip():
            st.warning("대화 제목을 입력한다.")
        else:
            create_conversation(api, title.strip())


def open_delete_dialog(conversation: dict):
    # 아이콘 클릭은 삭제할 대화를 선택해 확인 창만 연다.
    close_new_dialog()
    close_rename_dialog()
    st.session_state.ui_deleting_conversation = conversation


def close_delete_dialog():
    # 삭제 대상을 비워 확인 창을 닫는다.
    st.session_state.ui_deleting_conversation = None


def finish_conversation_delete(conversation_id: int, result: dict | None = None):
    # 삭제된 대화의 목록·선택·입력을 정리하고 결과 안내와 함께 화면을 다시 그린다.
    discard_conversation(conversation_id)
    notice = "대화와 메시지를 삭제했다."
    # DB 삭제와 백엔드 임시 데이터 정리는 별도 결과다. 정리 실패로 DELETE를 반복하지 않는다.
    if result and (not result["cache_cleared"] or not result["drafts_cleared"]):
        notice += " 남은 임시 데이터의 정리는 확인하지 못했다. 삭제는 완료했다."
    after_write(notice)


def delete_conversation(api: ApiClient, conversation: dict):
    # 삭제 확인을 받은 대화 ID로 DELETE하고 확인된 결과만 화면에 반영한다.
    conversation_id = conversation["id"]
    result = call(api, "DELETE", f"/conversations/{conversation_id}")
    if result is None:
        error = st.session_state.ui_last_error
        # API가 대화 부재를 응답한 경우에는 화면의 같은 ID도 정리한다.
        if error and error.status == 404 and error.code == "NOT_FOUND" and error.message == "대화 기록을 찾을 수 없다":
            finish_conversation_delete(conversation_id)
        return
    # 예상한 삭제 응답인지 확인한 뒤 목록에서 제거한다. 불확실한 응답은 직접 확인한다.
    if (result.get("deleted") is False or type(result.get("cache_cleared")) is not bool
            or type(result.get("drafts_cleared")) is not bool):
        st.error("대화 삭제 결과를 확인하지 못했다. 목록을 확인한 뒤 직접 다시 시도한다.")
        return
    finish_conversation_delete(conversation_id, result)




@st.dialog("대화 삭제", on_dismiss=close_delete_dialog)
def delete_conversation_dialog(api: ApiClient):
    # 경고를 보여 준다. 취소는 창만 닫고 삭제 확인을 누른 때 DELETE한다.
    conversation = st.session_state.ui_deleting_conversation
    if conversation is None:
        return
    st.write(conversation["title"])
    st.warning("이 대화와 대화 안의 모든 메시지를 영구 삭제한다. 삭제한 내용은 되돌릴 수 없다.")
    with st.container(key="ui_conversation_delete_actions", horizontal=True, horizontal_alignment="right"):
        cancelled = st.button("취소", key="ui_cancel_conversation_delete", width="content")
        confirmed = st.button("삭제", key="ui_confirm_conversation_delete", type="primary", width="content")
    if cancelled:
        close_delete_dialog()
        st.rerun()
    if confirmed:
        delete_conversation(api, conversation)


def open_rename_dialog(conversation: dict):
    # 제목 수정 창을 열고 해당 대화의 편집 중인 제목 또는 현재 제목을 넣는다.
    close_new_dialog()
    close_delete_dialog()
    st.session_state.ui_renaming_conversation = conversation
    st.session_state[f"ui_rename_title_{conversation['id']}"] = st.session_state.ui_rename_title_texts.get(conversation["id"], conversation["title"])


def close_rename_dialog():
    # 제목 수정 대상을 비워 창을 닫는다.
    st.session_state.ui_renaming_conversation = None


def apply_conversation_title(conversation: dict):
    # 응답 ID와 같은 대화의 목록·본문 제목을 함께 바꾼다.
    conversation_id = conversation["id"]
    # {**c, ...}는 기존 값을 복사한 뒤 title을 덮어쓴다. 다른 ID의 대화는 유지한다.
    st.session_state.ui_conversations = [({**c, "title": conversation["title"]} if c["id"] == conversation_id else c)
                                         for c in st.session_state.ui_conversations]
    selected = st.session_state.ui_conversation
    if selected and selected["id"] == conversation_id:
        st.session_state.ui_conversation = {**selected, "title": conversation["title"]}


def rename_conversation(api: ApiClient, conversation: dict, title: str):
    # 제목만 PATCH하고 요청한 ID·응답 제목을 확인한 뒤 화면에 반영한다.
    result = call(api, "PATCH", f"/conversations/{conversation['id']}", body={"title": title})
    if result is None:
        return
    if result.get("id") != conversation["id"] or not isinstance(result.get("title"), str):
        st.error("대화 제목 수정 결과를 확인하지 못했다. 목록을 확인한 뒤 직접 다시 시도한다.")
        return
    # 성공한 제목을 표시하고 편집 입력·창을 정리한다. 실패에서는 이 구간을 실행하지 않는다.
    apply_conversation_title(result)
    st.session_state.ui_rename_title_texts.pop(conversation["id"], None)
    close_rename_dialog()
    after_write("대화 제목을 수정했다.")




@st.dialog("대화 제목 수정", on_dismiss=close_rename_dialog)
def rename_conversation_dialog(api: ApiClient):
    # 제목 수정 폼의 제출·취소를 처리한다. 빈 제목은 PATCH하지 않는다.
    conversation = st.session_state.ui_renaming_conversation
    if conversation is None:
        return
    conversation_id = conversation["id"]
    # ID가 포함된 위젯 key로 대화마다 제목 입력을 구분한다.
    key = f"ui_rename_title_{conversation_id}"
    if key not in st.session_state:
        st.session_state[key] = st.session_state.ui_rename_title_texts.get(conversation_id, conversation["title"])
    with st.form(f"rename_conversation_{conversation_id}"):
        title = st.text_input("대화 제목", max_chars=100, key=key)
        with st.container(horizontal=True, horizontal_alignment="right"):
            submitted = st.form_submit_button("수정 저장", type="primary")
            cancelled = st.form_submit_button("취소")
    if cancelled:
        st.session_state.ui_rename_title_texts.pop(conversation_id, None)
        close_rename_dialog()
        st.rerun()
    if submitted:
        st.session_state.ui_rename_title_texts[conversation_id] = title
        if title.strip():
            rename_conversation(api, conversation, title.strip())
        else:
            st.warning("대화 제목을 입력한다.")


def select_initial_conversation(api: ApiClient):
    # 첫 진입·선택 대화 삭제 뒤에는 목록의 첫 대화를 연다. 기존 선택은 페이지 왕복에도 유지한다.
    if (st.session_state.ui_conversation is None
            and st.session_state.ui_conversations_status == "ready"
            and st.session_state.ui_conversations):
        open_conversation(api, st.session_state.ui_conversations[0])


def request_conversation(conversation: dict):
    # 클릭은 이동 의도만 기록한다. 진입점의 계정 확인 뒤 목록을 그리기 전에 실제로 이동한다.
    st.session_state.ui_open_conversation = conversation


def conversation_rows(api: ApiClient):
    # 제목은 대화 선택·조회로, 아이콘은 수정·삭제 확인 창을 여는 동작으로 연결한다.
    for conversation in st.session_state.ui_conversations:
        selected = st.session_state.ui_conversation
        active = selected and selected["id"] == conversation["id"]
        prefix = "ui_conversation_active_" if active else "ui_conversation_row_"
        with st.container(key=f"{prefix}{conversation['id']}"):
            # 제목 열은 남는 폭을 쓰고 두 아이콘 열은 고정한다. 전체 제목은 도움말로 확인한다.
            title_column, rename_column, delete_column = st.columns([1, .15, .15], gap="small", vertical_alignment="center")
            with title_column:
                st.button(conversation["title"], key=f"ui_conversation_{conversation['id']}",
                          use_container_width=True, help=conversation["title"],
                          on_click=request_conversation, args=(conversation,))
            with rename_column:
                st.button("", icon=":material/edit:", key=f"ui_conversation_rename_{conversation['id']}",
                          help="대화 제목 수정", width="content", on_click=open_rename_dialog, args=(conversation,))
            with delete_column:
                st.button("", icon=":material/delete:", key=f"ui_conversation_delete_{conversation['id']}",
                          help="대화 삭제", width="content", on_click=open_delete_dialog, args=(conversation,))
    cursor = st.session_state.ui_conversation_cursor
    if cursor is not None and st.button("이전 대화 더 보기", key="ui_older_conversations"):
        # 이 버튼은 목록을 그린 뒤 실행된다. 재실행해 이번 조회 결과를 즉시 반영한다.
        if load_conversations(api, older=True):
            st.rerun()


def conversation_list(api: ApiClient):
    # 사이드바 목록을 표시하고 제목 선택·관리 아이콘을 각 동작에 연결한다.
    target = st.session_state.pop("ui_open_conversation", None)
    if target is not None:
        # 선택 표시와 본문을 같은 실행에서 바꾼다. 두 번째 전체 실행은 필요하지 않다.
        open_conversation(api, target)
    # 가로 컨테이너는 제목과 작은 버튼을 나란히 두며 좁은 화면에서도 버튼 폭을 유지한다.
    with st.container(horizontal=True, vertical_alignment="center"):
        st.subheader("대화")
        st.button("＋", key="ui_new_conversation", help="새 대화 만들기", width="content",
                  on_click=open_new_dialog)
    # 조회 전이면 GET한다. 조회 시도 여부를 기억해 일반 위젯 재실행마다 반복하지 않는다.
    if not st.session_state.ui_conversations_attempted:
        load_conversations(api)
    if st.session_state.ui_conversations_status == "error":
        if st.button("다시 불러오기", key="ui_retry_conversations"):
            if load_conversations(api):
                st.rerun()
    select_initial_conversation(api)
    # 메뉴와 새 대화 버튼은 목록 밖에 두고, 긴 목록만 별도로 스크롤한다.
    with st.container(key="release_conversation_list", border=False):
        conversation_rows(api)
    # ready의 빈 목록은 실제 대화가 없는 상태다. 조회 오류와 구분해 안내한다.
    if not st.session_state.ui_conversations:
        if st.session_state.ui_conversations_status == "ready":
            st.caption("첫 대화를 시작한다.")
        elif st.session_state.ui_conversations_status == "error":
            st.caption("대화 목록을 불러오지 못했다. 다시 확인한다.")
    if st.session_state.ui_new_conversation_open:
        new_conversation_dialog(api)
    elif st.session_state.ui_deleting_conversation is not None:
        delete_conversation_dialog(api)
    elif st.session_state.ui_renaming_conversation is not None:
        rename_conversation_dialog(api)


def merge_conversations(existing: list[dict], received: list[dict]) -> list[dict]:
    # ID를 key로 합친다. 같은 ID는 뒤에 받은 응답으로 교체하고 중복을 없앤다.
    records = {c["id"]: c for c in existing + received}
    # ID 내림차순으로 최근 대화를 먼저 표시한다.
    return sorted(records.values(), key=lambda c: c["id"], reverse=True)




def create_conversation(api: ApiClient, title: str):
    if not flush_before_navigation(api):
        return
    result = call(api, "POST", "/me/conversations", body={"title": title})
    if result is None:
        return
    # 생성 응답의 대화를 기존 목록에 합치고 서버가 반환한 ID로 선택한다.
    st.session_state.ui_conversations = merge_conversations(st.session_state.ui_conversations, [result])
    select_conversation(result)
    # initialize가 다음 실행에서 제목 입력을 비우도록 표시하고 생성 창을 닫는다.
    st.session_state.ui_clear_title = True
    close_new_dialog()
    st.session_state.ui_notice = "대화를 만들었다. 질문을 입력할 수 있다."
    st.rerun()
