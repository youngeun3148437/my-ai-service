"""전송은 버튼, 초안은 변경·시간·이동 조건에 연결한다."""
import streamlit as st
from clients.api_client import ApiError
from ui.ai import finish_pending
from ui.ai_views import options
from ui.chat_updates import request_send, rerun_chat


def flush_before_navigation(api) -> bool:
    # 앱 안에서 다른 대화·페이지로 이동하기 전에 저장한다. 실패하면 현재 화면을 유지한다.
    previous = st.session_state.ui_conversation
    if previous is None:
        return True
    identifier = previous['id']
    state = st.session_state.ui_draft_states.get(identifier)
    if st.session_state.ui_pending_messages.get(identifier) or (state and state.paused):
        st.warning('저장 결과와 입력을 먼저 확인한다. 현재 대화를 유지한다.')
        return False
    # 이동 클릭과 함께 도착한 최신 위젯 값을 이전 대화의 초안에 먼저 반영한다.
    latest = st.session_state.get(f'ui_question_{identifier}')
    if state and isinstance(latest, str) and latest != state.text:
        from state.draft_state import DraftController
        DraftController(api, identifier, state).change(latest)
    if state and state.changed_at is not None:
        from state.draft_state import DraftController
        # 이동할 때는 지연 시간을 기다리지 않는다. 브라우저 종료 순간의 저장은 보장하지 않는다.
        DraftController(api, identifier, state).save(force=True)
        return not state.paused
    return True


def prepare_composer(api, conversation_id: int):
    # 최초 대화 표시 전에 필요한 조회만 준비한다. 주기 실행에서는 이미 읽은 상태를 재사용한다.
    from state.draft_state import DraftState, DraftController
    states = st.session_state.ui_draft_states
    state = states.setdefault(conversation_id, DraftState(text=st.session_state.ui_question_texts.get(conversation_id, '')))
    controller = DraftController(api, conversation_id, state)
    key = f'ui_question_{conversation_id}'
    if not state.loaded and not state.paused:
        # 위젯을 만들기 전에 활성 초안을 읽어 저장된 글·ID·revision을 입력 상태에 연결한다.
        controller.load()
        if state.loaded:
            st.session_state[key] = state.text
    if key not in st.session_state:
        st.session_state[key] = state.text
    pending_input = st.session_state.get('ui_pending_question')
    if pending_input and pending_input[0] == conversation_id:
        st.session_state.pop('ui_pending_question')
        st.session_state[key] = pending_input[1]
        st.session_state.ui_question_texts[conversation_id] = pending_input[1]
    from ui.attachment_views import prepare_attachments
    prepare_attachments(api, conversation_id)
    return state, controller


@st.fragment(run_every=2, key="release_chat_composer")
def draft_composer(api, conversation_id: int):
    # 이 입력 구역만 주기 실행한다. 전송 버튼은 부모 대화 fragment에 요청해 기록도 함께 바꾼다.
    state, controller = prepare_composer(api, conversation_id)
    key = f'ui_question_{conversation_id}'
    busy = conversation_id in st.session_state.ui_queued_answers
    pending = st.session_state.ui_pending_messages.get(conversation_id)
    from ui.attachment_views import attachments, question_attachments, state_for
    with st.container(key='release_composer', border=True):
        question = st.text_area('질문', max_chars=5000, height=110, key=key, on_change='ignore',
                                label_visibility='collapsed', disabled=busy or bool(pending),
                                placeholder='메시지를 입력하세요…')
        question_attachments(conversation_id, disabled=busy or bool(pending))
        tools, send = st.columns([4, 1], vertical_alignment='center')
        with tools:
            with st.container(key='release_composer_tools', horizontal=True, vertical_alignment='center'):
                attachments(api, conversation_id, disabled=busy or bool(pending))
                style, delete_requested = options(disabled=busy or bool(pending), draft=state,
                                                   conversation_id=conversation_id)
                st.caption('간결하게' if style == 'brief' else '예시와 함께')
        with send:
            with st.container(horizontal=True, horizontal_alignment='right'):
                st.button('', key='ui_send_message', icon=':material/arrow_upward:', type='primary',
                                      help='같은 전송 재시도' if pending else '질문 전송',
                                      disabled=busy, width='content', on_click=request_send, args=(conversation_id,))
    st.session_state.ui_question_texts[conversation_id] = question
    # 콜백 대신 이번 실행의 입력값을 비교한다. change는 변경 시각만 기록하고 즉시 저장하지 않는다.
    if question != state.text:
        controller.change(question)
    # 명시적 전송·삭제 클릭에는 자동 저장을 먼저 호출하지 않는다.
    if delete_requested:
        if controller.discard():
            st.session_state.ui_question_texts[conversation_id] = ''
            st.session_state.ui_pending_question = (conversation_id, '')
            rerun_chat()
    elif not pending:
        controller.save()
    if state.record:
        st.session_state.ui_draft_ids[conversation_id] = state.record['session_id']
    else:
        st.session_state.ui_draft_ids.pop(conversation_id, None)
    if state.changed_at is not None and not state.paused:
        st.caption('저장 대기')
    elif state.status in ('저장됨','복원됨'):
        st.caption(state.status)
    attachment = state_for(conversation_id)
    if attachment.pending or attachment.error:
        st.caption('첨부 작업을 확인해야 한다. 📎에서 저장 결과와 원본 목록을 확인한다.')
    if pending:
        st.warning('전송 응답을 확인하지 못했다. 같은 전송 재시도로 저장 여부를 확인한다.')
        with st.expander('전송 결과 확인'):
            checked = st.checkbox('최근 기록을 확인했다', key=f'ui_confirm_send_reset_{conversation_id}')
            if st.button('이전 전송 종료', disabled=not checked, key='ui_reset_send'):
                finish_pending(conversation_id)
                rerun_chat()
    if state.paused:
        st.warning(state.error or '입력을 유지했다. 저장본과 비교한다.')
        if st.button('저장본 확인', key='ui_restore_draft'):
            controller.check_remote()
        if state.remote_known:
            remote = state.remote['draft_question'] if state.remote else ''
            st.text_area('저장된 글', value=remote, disabled=True, key=f'ui_remote_{conversation_id}')
            with st.container(horizontal=True, horizontal_alignment='right'):
                server = st.button('저장된 글 사용', key='ui_use_server_draft')
                local = st.button('내 글로 다시 저장', key='ui_keep_local_draft')
            if server or local:
                controller.resolve(use_server=server)
                st.session_state.ui_pending_question = (conversation_id, state.text)
                rerun_chat()
    if st.session_state.ui_tokens is None:
        st.rerun()


def composer(api, conversation_id: int):
    draft_composer(api, conversation_id)
