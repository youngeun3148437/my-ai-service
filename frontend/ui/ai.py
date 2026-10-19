"""최신 질문 저장과 저장된 질문의 답변 생성을 순서대로 연결한다."""
import streamlit as st
from clients.api_client import ApiError
from ui.chat_updates import after_chat_write as after_write, rerun_chat


def save_question(api, conversation_id: int, content: str, extra: dict) -> dict:
    # 지금 입력한 role·content를 보낸다. 저장된 초안에서 전송 글을 가져오지 않는다.
    body = {'role': 'user', 'content': content.strip(), **extra}
    from state.message_state import MessageDelivery
    return MessageDelivery(api, st.session_state.ui_pending_messages).send(conversation_id, body)


def validate_message(result: dict):
    row = result.get('message') if isinstance(result, dict) else None
    if (not isinstance(row, dict) or type(row.get('id')) is not int
            or row.get('role') not in ('user', 'assistant') or not isinstance(row.get('content'), str)
            or type(result.get('cache_cleared')) is not bool):
        raise ApiError(502, 'RESPONSE_ERROR', '저장 결과를 확인하지 못했다. 원본을 먼저 확인한다.')


def merge_saved(row: dict):
    # 저장 응답을 먼저 보여 준다. 후속 GET 실패로 이 내용을 지우지 않는다.
    page = st.session_state.ui_page or {'messages': [], 'next_before_id': None}
    records = {item['id']: item for item in page['messages']}
    records[row['id']] = row
    st.session_state.ui_page = {**page, 'messages': sorted(records.values(), key=lambda item: item['id'])}
    st.session_state.ui_messages_attempted = True


def refresh_saved_question(api, conversation_id: int, result: dict) -> str:
    # 저장 응답과 뒤의 조회 결과를 구분한다. 조회 실패로 질문을 다시 POST하지 않는다.
    merge_saved(result['message'])
    if not result['cache_cleared']:
        return '최신 기록 갱신이 지연되고 있다. 저장한 질문은 유지한다.'
    from ui.messages import refresh_messages, merge_refreshed_messages
    saved = st.session_state.ui_page
    if not refresh_messages(api, conversation_id):
        return ' 저장 결과는 유지했다. 기록을 다시 불러온다. 질문을 다시 보내지 않는다.'
    merge_refreshed_messages(saved)
    return ''


def apply_answer(api, conversation_id: int, result: dict) -> str:
    # 답변 저장 응답을 확인한 뒤 목록을 바꾸고, 이 질문의 생성 대기를 끝낸다.
    validate_message(result)
    merge_saved(result['message'])
    st.session_state.ui_answer_targets.pop(conversation_id, None)
    notice = '답변을 저장했다.'
    if result['cache_cleared']:
        from ui.messages import refresh_messages
        saved = st.session_state.ui_page
        if refresh_messages(api, conversation_id):
            fresh = st.session_state.ui_page
            rows = {row['id']: row for row in saved['messages'] + fresh['messages']}
            st.session_state.ui_page = {**fresh, 'messages': sorted(rows.values(), key=lambda row: row['id']),
                                        'next_before_id': saved['next_before_id']}
        else:
            notice += ' 저장 결과는 유지했다. 기록을 다시 불러온다.'
    else:
        notice += ' 최신 기록 갱신이 지연되고 있다. 저장한 답변은 유지한다.'
    return notice


def send_question(api, conversation_id: int, content: str, *, rerun: bool = True):
    # 질문 저장 실패와 질문 저장 후 생성 실패는 서로 다른 결과다.
    if not content.strip():
        return
    extra = {}
    state = st.session_state.ui_draft_states.get(conversation_id)
    if state and state.record:
        extra = {'session_id': state.record['session_id'], 'session_revision': state.record['revision']}
    if conversation_id not in st.session_state.ui_pending_message_attachments:
        from ui.attachment_views import state_for
        # 선택 위젯은 composer에서 이미 만들었다. 전송에서는 값을 복사해 질문에 고정한다.
        attachment = state_for(conversation_id)
        ready = {row['id'] for row in attachment.records or [] if row['status'] == 'ready'}
        st.session_state.ui_pending_message_attachments[conversation_id] = [
            identifier for identifier in attachment.selected_ids if identifier in ready]
    try:
        result = save_question(api, conversation_id, content, extra)
        validate_message(result)
    except ApiError as error:
        st.session_state.ui_last_error = error
        st.error(error.message)
        st.warning('입력은 유지했다. 기록을 확인한 뒤 직접 다시 시도한다.')
        return
    row = result['message']
    refresh_notice = refresh_saved_question(api, conversation_id, result)
    from clients.chat_scroll import request_latest
    request_latest(conversation_id)
    # 성공한 질문만 입력을 비운다. 다음 rerun의 위젯 생성 전에 비우기를 적용한다.
    st.session_state.ui_question_texts[conversation_id] = ''
    st.session_state.ui_pending_question = (conversation_id, '')
    from state.draft_state import DraftController
    draft = st.session_state.ui_draft_states.get(conversation_id)
    if draft:
        DraftController(api, conversation_id, draft).sent()
    st.session_state.ui_draft_ids.pop(conversation_id, None)
    from ui.attachment_views import question_sent
    question_sent(conversation_id, row['id'])
    st.session_state.ui_answer_targets[conversation_id] = row['id']
    notice = refresh_notice
    if result.get('draft_cleanup') in ('unavailable', 'changed'):
        notice += ' 다른 화면의 초안 또는 확인하지 못한 정리 결과가 있다. 질문을 다시 보내지 않는다.'
    options = st.session_state.ui_ai_options
    if not result.get('replayed', False):
        # 먼저 재실행해 질문을 기록에 표시한다. 생성은 그 기록 영역에서 한 번 실행한다.
        queue_answer(conversation_id, row['id'], options['ui_answer_style'],
                     True, notice=notice)
    elif result.get('replayed', False):
        # 재사용한 질문은 기존 답변을 확인한 뒤 사용자가 직접 다시 생성한다.
        notice += ' 이전 전송의 저장 결과다. 기존 답변을 먼저 확인한다.'
    if rerun:
        after_write(notice)
    else:
        st.session_state.ui_chat_notice = notice


def queue_answer(conversation_id: int, question_id: int, style: str, history: bool, *, notice: str = ''):
    # 입력 영역에서는 생성 작업만 기록한다. 다음 실행의 대화 기록 안에서 답변을 받는다.
    st.session_state.ui_queued_answers[conversation_id] = (question_id, style, history, notice)


def retry_answer(api, conversation_id: int, question_id: int, style: str, history: bool):
    # done 응답이 유실되어도 답변 INSERT는 끝났을 수 있다. 먼저 최신 원본을 읽는다.
    from ui.messages import refresh_messages, merge_refreshed_messages
    previous = st.session_state.ui_page
    if not refresh_messages(api, conversation_id, original=True):
        # 읽기 실패는 답변 부재가 아니다. 입력·대기 질문을 유지하고 AI POST를 시작하지 않는다.
        return
    latest = st.session_state.ui_page
    records = latest["messages"]
    if previous:
        merge_refreshed_messages(previous)
    question = next((row for row in records if row["id"] == question_id and row["role"] == "user"), None)
    if question is None:
        # 최신 페이지 밖의 질문을 빈 답변으로 추정하지 않는다. 원본을 더 확인하도록 멈춘다.
        return after_write("이 질문은 최신 기록 범위에서 확인되지 않았다. 기록을 확인한 뒤 최신 질문을 선택한다.")
    following = [row for row in records if row["id"] > question_id]
    if any(row["role"] == "assistant" for row in following):
        # 메시지에는 질문-답변 FK가 없다. 후속 답변이 있으면 보수적으로 새 생성을 막는다.
        st.session_state.ui_answer_targets.pop(conversation_id, None)
        st.session_state.ui_partial_answers.pop(conversation_id, None)
        return after_write("질문 뒤에 저장된 답변을 불러왔다. 새 답변 생성 요청은 보내지 않았다.")
    if following:
        return after_write("이 질문 뒤에 다른 기록이 있다. 최신 기록을 확인한 뒤 답변을 요청한다.")
    # 최신 원본의 마지막 질문에만 사용자가 직접 새 답변을 요청한다. 질문 POST는 반복하지 않는다.
    from clients.chat_scroll import request_latest
    request_latest(conversation_id)
    queue_answer(conversation_id, question_id, style, history)
    rerun_chat()


def finish_pending(conversation_id: int):
    # 원본 확인 뒤 화면의 이전 전송 작업을 명시적으로 끝낸다.
    st.session_state.ui_pending_messages.pop(conversation_id, None)
    st.session_state.ui_pending_message_attachments.pop(conversation_id, None)
