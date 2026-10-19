"""입력·저장본·충돌 상태를 보관하고 변경된 초안만 지연 저장한다."""

from dataclasses import dataclass
from time import monotonic

from clients.api_client import ApiError

SAVE_DELAY_SECONDS = 1.5


@dataclass
class DraftState:
    text: str = ''
    saved_text: str = ''
    record: dict | None = None
    remote: dict | None = None
    remote_known: bool = False
    loaded: bool = False
    changed_at: float | None = None
    paused: bool = False
    status: str = '준비'
    error: str = ''


class DraftController:
    def __init__(self, api, conversation_id: int, state: DraftState, clock=None):
        self.api, self.state = api, state
        self.path = f'/conversations/{conversation_id}/work-sessions'
        self.clock = clock or monotonic

    def load(self):
        # 활성 초안 하나를 조회한다. 화면의 미전송 입력이 있으면 저장본으로 덮어쓰지 않는다.
        try:
            record = self.api.request('GET', self.path)['draft']
        except ApiError as error:
            self.fail(error)
            return
        self.state.loaded = True
        self.state.record = record
        if not self.state.text:
            self.state.text = record['draft_question'] if record else ''
        self.state.saved_text = record['draft_question'] if record else ''
        self.state.status = '복원됨' if record else '초안 없음'

    def change(self, text: str):
        # 전달된 글이 마지막 저장본과 다르면 지연 저장의 시작 시각을 기록한다.
        self.state.text = text
        self.state.changed_at = self.clock() if text.strip() != self.state.saved_text.strip() else None
        if not self.state.paused and self.state.changed_at is not None:
            self.state.status = '저장 대기'

    def fail(self, error: ApiError):
        # 연결 오류의 저장 여부는 미확정이다. 주기 실행으로 같은 쓰기를 반복하지 않는다.
        self.state.paused = True
        self.state.error = f'{error.code}: {error.message}'
        self.state.status = '충돌' if error.status == 409 else '만료' if error.status == 404 else '확인 필요'
        if error.status in (404, 409):
            self.check_remote()

    def check_remote(self):
        try:
            self.state.remote = self.api.request('GET', self.path)['draft']
            self.state.remote_known = True
        except ApiError as error:
            self.state.remote_known = False
            self.state.error = f'{error.code}: {error.message}'

    def save(self, *, force=False):
        # 정상 조회와 지연 조건을 통과한 입력만 저장한다. force는 앱 이동 전 저장에 사용한다.
        state = self.state
        if state.paused or not state.loaded:
            return
        if not force and (state.changed_at is None or self.clock() - state.changed_at < SAVE_DELAY_SECONDS):
            return
        content = state.text.strip()
        try:
            record = self.write_record(content)
        except ApiError as error:
            self.fail(error)
            return
        # 저장 응답이 확인된 뒤에만 ID·revision·저장 기준 글을 바꾼다. 실패한 입력은 유지한다.
        state.record, state.saved_text, state.changed_at = record, state.text, None
        state.status, state.error = ('저장됨' if record else '초안 없음'), ''
        state.remote, state.remote_known = None, False

    def resolve(self, *, use_server: bool):
        # 최신 저장본을 읽고 사용자가 선택한 뒤에만 충돌 정지를 해제한다.
        if not self.state.remote_known:
            return
        state = self.state
        state.record = state.remote
        state.loaded = True
        state.saved_text = state.remote['draft_question'] if state.remote else ''
        state.paused, state.error = False, ''
        if use_server:
            state.text, state.changed_at, state.status = state.saved_text, None, '복원됨'
        else:
            # 사용자가 최신 저장본을 비교한 뒤 명시적으로 재저장한다. 다시 충돌할 수도 있다.
            state.changed_at = self.clock()
            self.save(force=True)

    def sent(self):
        self.state.text = self.state.saved_text = ''
        self.state.record = self.state.remote = None
        self.state.changed_at = None
        self.state.loaded, self.state.remote_known = True, False
        self.state.paused = False
        self.state.status = '전송 완료'
        self.state.error = ''

    def write_record(self, content: str):
        # 본문과 현재 저장본 유무를 받아 POST 또는 revision을 포함한 PATCH를 선택한다.
        state = self.state
        if not content:
            if state.record:
                # 입력을 모두 비우면 저장된 초안만 같은 revision으로 정리한다.
                record = state.record
                self.api.request('DELETE', f"{self.path}/{record['session_id']}",
                                 params={'revision': record['revision']})
            return None
        if state.record:
            return self.api.request('PATCH', f"{self.path}/{state.record['session_id']}",
                body={'draft_question': content, 'revision': state.record['revision']})
        return self.api.request('POST', self.path, body={'draft_question': content})

    def discard(self) -> bool:
        # 명시적 삭제의 성공·실패 상태는 자동 저장과 같은 규칙으로 처리한다.
        if self.state.record is None:
            return True
        try:
            self.delete_current()
        except ApiError as error:
            self.fail(error)
            return False
        self.sent()
        self.state.status = '초안 없음'
        return True

    def delete_current(self):
        # 직접 삭제한 현재 초안의 ID·revision으로 DELETE를 요청한다.
        record = self.state.record
        return self.api.request('DELETE',f"{self.path}/{record['session_id']}",params={'revision':record['revision']})
