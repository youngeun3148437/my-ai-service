"""파일 선택·명시적 업로드·비공개 원본 확인과 질문에 사용할 첨부 선택을 표시한다."""

from base64 import b64encode
from html import escape
from io import BytesIO

import streamlit as st
from PIL import Image, UnidentifiedImageError

from clients.api_client import ApiError
from state.attachment_state import AttachmentController, AttachmentState


def state_for(conversation_id: int) -> AttachmentState:
    states = st.session_state.ui_attachment_states
    if conversation_id not in states:
        states[conversation_id] = AttachmentState()
    return states[conversation_id]


def show_error(state: AttachmentState, error: ApiError):
    state.error = f"{error.code}: {error.message}"
    if error.code == "AUTH_SESSION_ENDED":
        st.session_state.ui_notice = error.message
        st.rerun()


def preview(content: bytes, content_type: str):
    if content_type.startswith("image/"):
        try:
            # 확장자 필터는 파일 내용 검증이 아니다. 큰 이미지의 미리보기부터 제한한다.
            with Image.open(BytesIO(content)) as image:
                if image.width * image.height > 20_000_000:
                    st.warning("이미지는 2천만 픽셀 이하여야 한다.")
                    return
                if image.format not in ("PNG", "JPEG"):
                    st.warning("PNG·JPEG 이미지 내용을 확인한다.")
                    return
                mime = "image/png" if image.format == "PNG" else "image/jpeg"
            # st.image의 공유 media URL 대신 현재 화면에만 파일 bytes를 전달한다.
            encoded = b64encode(content).decode("ascii")
            st.html(f'<img src="data:{mime};base64,{encoded}" alt="첨부 이미지" '
                    'style="width:360px;max-width:100%;height:auto;border-radius:8px">')
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            st.warning("이미지 미리보기를 열 수 없다. 파일 내용을 확인한다.")
    else:
        st.caption("PDF 원본은 내려받아 확인한다. AI에는 선택한 PDF 파일 자체를 전달한다.")


def private_download(content: bytes, record: dict):
    mime = record["content_type"]
    if mime not in ("image/png", "image/jpeg", "application/pdf"):
        st.warning("다운로드할 파일 형식을 확인한다.")
        return
    # download_button도 공유 media URL을 만든다. iframe 안의 data URI는 별도 서버 파일 URL이 없다.
    # 파일명은 HTML 속성에 들어가므로 따옴표·태그를 escape한다. 스크립트는 넣지 않는다.
    filename = escape(record["filename"], quote=True)
    encoded = b64encode(content).decode("ascii")
    st.iframe(
        '<style>body{margin:0;padding:0}</style>'
        f'<a href="data:{mime};base64,{encoded}" download="{filename}" '
        'style="display:inline-block;margin:2px 0;padding:8px 12px;border:1px solid #d0d5dd;'
        'border-radius:8px;text-decoration:none;color:#182230;font:14px system-ui;background:white">'
        '원본 다운로드</a>', height=48, alt="첨부 원본 다운로드")


def close_attachment_delete():
    st.session_state.ui_deleting_attachment = None


def prepare_attachments(api, conversation_id: int):
    # 목록만 먼저 읽는다. 파일 원본은 사용자가 열기를 누른 뒤 요청한다.
    state = state_for(conversation_id)
    if state.records is None and not state.error:
        try:
            AttachmentController(api, conversation_id, state).refresh()
        except ApiError as error:
            show_error(state, error)


def attachment_delete_confirmation(api, conversation_id: int):
    deleting = st.session_state.get("ui_deleting_attachment")
    if not deleting or deleting[0] != conversation_id:
        return
    record = deleting[1]
    st.warning(f"‘{record['filename']}’ 원본을 영구 삭제한다. 이 작업은 되돌릴 수 없다.")
    with st.container(horizontal=True, horizontal_alignment="right"):
        if st.button("취소", key="ui_cancel_attachment_delete"):
            close_attachment_delete()
            st.rerun()
        if st.button("삭제", type="primary", key="ui_confirm_attachment_delete"):
            try:
                AttachmentController(api, conversation_id, state_for(conversation_id)).delete(record["id"])
            except ApiError as error:
                show_error(state_for(conversation_id), error)
            else:
                close_attachment_delete()
            st.rerun()


def upload_controls(controller, conversation_id: int, *, disabled: bool):
    state = controller.state
    st.caption("PNG·JPEG·PDF · 파일당 5 MiB 이하 · PDF 20페이지 이하")
    uploaded = st.file_uploader("첨부할 파일", type=["png", "jpg", "jpeg", "pdf"],
                                key=f"ui_attachment_upload_{conversation_id}_{state.uploader_version}",
                                max_upload_size=5, disabled=disabled or state.pending is not None)
    if uploaded is not None and state.pending is None:
        try:
            controller.select_file(uploaded.name, uploaded.getvalue())
        except ApiError as error:
            state.chosen = None
            show_error(state, error)
    current = state.pending or state.chosen
    if current:
        st.caption(f"{current['filename']} · {len(current['content']) / 1024:.1f} KiB")
        preview(current["content"], current["content_type"])
    with st.container(horizontal=True, horizontal_alignment="right"):
        save = st.button("같은 파일 저장 재시도" if state.pending else "첨부 저장",
                         disabled=disabled or current is None, type="primary", key=f"ui_save_attachment_{conversation_id}")
    if save:
        try:
            with st.spinner("첨부 저장 중"):
                controller.save()
        except ApiError as error:
            show_error(state, error)
        st.rerun()
    if state.pending:
        st.warning("저장 결과를 확인하지 못했다. 같은 파일 저장 재시도는 처음의 번호와 내용을 사용한다.")
        checked = st.checkbox("원본 목록 확인 후 업로드 작업 종료", key=f"ui_upload_reset_confirm_{conversation_id}")
        if st.button("업로드 작업 종료", disabled=disabled or not checked, key=f"ui_upload_reset_{conversation_id}"):
            controller.finish_pending()
            st.rerun()


def remember_selection(conversation_id: int, version: int):
    state = state_for(conversation_id)
    # 전송 전에 고른 값이 늦게 도착해도 다음 질문의 첨부 선택을 되살리지 않는다.
    if version == state.selection_version:
        state.selected_ids = list(st.session_state[f"ui_attachment_selection_{conversation_id}_{version}"])


def saved_attachments(controller, conversation_id: int, *, disabled: bool):
    state = controller.state
    records = state.records
    if records is None:
        st.caption("첨부 목록을 확인하지 못했다. 다시 불러오기를 사용한다.")
        return
    if not records:
        st.caption("저장된 첨부가 없다. 파일을 올리면 이 대화에서 다시 선택할 수 있다.")
        return
    ready = {row["id"]: row for row in records if row["status"] == "ready"}
    version = state.selection_version
    key = f"ui_attachment_selection_{conversation_id}_{version}"
    # 위젯이 사라졌다 돌아와도 독립된 첨부 상태에서 선택을 복원한다.
    st.session_state[key] = [identifier for identifier in state.selected_ids if identifier in ready]
    selected = st.multiselect("이번 질문에 사용할 첨부", list(ready),
                              format_func=lambda identifier: ready[identifier]["filename"],
                              key=key, max_selections=2, disabled=disabled,
                              on_change=remember_selection, args=(conversation_id, version))
    state.selected_ids = list(selected)
    st.caption("최대 2개. 이전 질문의 파일은 자동으로 다시 보내지 않는다.")
    st.divider()
    for record in records:
        identifier = record["id"]
        with st.container(border=False):
            st.markdown(f"**{escape(record['filename'])}**")
            st.caption(f"{record['byte_size'] / 1024:.1f} KiB · {record['status']}")
            if record["status"] != "ready":
                st.info("저장 중인 파일이다. 같은 업로드 번호·파일로 저장을 완료하거나 삭제한다.")
            with st.container(horizontal=True, horizontal_alignment="right"):
                reading = st.button("원본 보기", disabled=record["status"] != "ready", type="tertiary",
                                    icon=":material/visibility:", key=f"ui_read_attachment_{identifier}")
                deleting = st.button("", icon=":material/delete:", help="첨부 원본 삭제", type="tertiary",
                                     disabled=disabled, key=f"ui_delete_attachment_{identifier}")
            if reading:
                try:
                    controller.read(identifier)
                except ApiError as error:
                    show_error(state, error)
            if identifier in state.contents:
                content = state.contents[identifier]
                preview(content, record["content_type"])
                private_download(content, record)
            if deleting:
                st.session_state.ui_deleting_attachment = (conversation_id, record)
            if st.session_state.get("ui_deleting_attachment") == (conversation_id, record):
                attachment_delete_confirmation(controller.api, conversation_id)


def attachments(api, conversation_id: int, *, disabled: bool = False):
    state = state_for(conversation_id)
    controller = AttachmentController(api, conversation_id, state)
    with st.popover("", icon=":material/attach_file:", help="파일 첨부·관리", type="tertiary", disabled=disabled):
        st.markdown("**파일 첨부**")
        upload_tab, saved_tab = st.tabs(["파일 올리기", "저장된 파일"])
        with upload_tab:
            upload_controls(controller, conversation_id, disabled=disabled)
        with saved_tab:
            if st.button("", icon=":material/refresh:", help="첨부 다시 불러오기", type="tertiary",
                         key=f"ui_list_attachments_{conversation_id}"):
                try:
                    controller.refresh()
                except ApiError as error:
                    show_error(state, error)
            saved_attachments(controller, conversation_id, disabled=disabled)
        if state.notice:
            st.success(state.notice)
            state.notice = ""
        if state.error:
            st.error(state.error)


def remove_selection(conversation_id: int, attachment_id: str):
    # ×는 이번 질문의 선택만 해제한다. 원본 DELETE나 업로드 요청을 보내지 않는다.
    state = state_for(conversation_id)
    state.selected_ids = [identifier for identifier in state.selected_ids if identifier != attachment_id]


def question_attachments(conversation_id: int, *, disabled: bool = False) -> list:
    state = state_for(conversation_id)
    pending = st.session_state.ui_pending_messages.get(conversation_id)
    records = {row["id"]: row for row in state.records or [] if row["status"] == "ready"}
    if state.records is not None:
        state.selected_ids = [identifier for identifier in state.selected_ids if identifier in records]
    # 전송 결과가 불확실할 때는 처음 요청의 첨부를 고정한다.
    identifiers = (list(st.session_state.ui_pending_message_attachments.get(conversation_id, []))
                   if pending else list(state.selected_ids))
    if identifiers:
        with st.container(key=f"release_attachment_chips_{conversation_id}", horizontal=True):
            for identifier in identifiers:
                filename = records.get(identifier, {}).get("filename", "첨부 파일")
                st.button(filename, icon=":material/close:", icon_position="right", width="content",
                          help="이번 질문의 첨부에서 제외 · 원본은 유지", disabled=disabled or bool(pending),
                          key=f"ui_unselect_attachment_{conversation_id}_{identifier}",
                          on_click=remove_selection, args=(conversation_id, identifier))
    return identifiers


def question_sent(conversation_id: int, question_id: int):
    identifiers = st.session_state.ui_pending_message_attachments.pop(conversation_id, [])
    st.session_state.ui_answer_attachments[(conversation_id, question_id)] = list(identifiers)
    state = state_for(conversation_id)
    state.selected_ids = []
    # 새 질문은 새 선택 위젯을 사용한다. 이전 위젯의 지연된 값은 별도로 격리한다.
    state.selection_version += 1
