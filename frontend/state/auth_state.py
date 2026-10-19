"""서버 로그인 세션의 갱신과 안전한 조회 재요청을 연결한다."""

from time import monotonic

from clients.api_client import ApiClient, ApiError


def end_session(state):
    # 인증 종료를 발견한 실행에서 이전 토큰의 알림 연결도 즉시 닫는다.
    realtime = state.get('ui_realtime_state')
    if realtime is not None:
        realtime.stop()
    state['ui_tokens'] = None
    state['ui_token_deadline'] = 0.0


def renew_tokens(base_url: str, state, *, force=False, verify=False, clock=None) -> bool:
    # 갱신 토큰은 FastAPI만 보관한다. 화면은 복원한 서버 세션으로만 갱신한다.
    if not state.get('lr_session_id'):
        end_session(state)
        raise ApiError(401, 'AUTH_SESSION_ENDED', '다시 로그인한다.')
    from state.login_session_state import renew_persistent
    return renew_persistent(base_url, state, force=force, verify=verify, clock=clock)


class AuthenticatedClient(ApiClient):
    def __init__(self, base_url: str, state, clock=None):
        super().__init__(base_url)
        self.state, self.clock = state, clock or monotonic

    def request(self, method, path, **kwargs):
        # 보호 요청 전에 수명·최근 세션 확인을 검사한다. 연속 요청은 같은 확인을 공유한다.
        renew_tokens(self.base_url, self.state, clock=self.clock)
        try:
            return ApiClient(self.base_url, self.state['ui_tokens']['access_token']).request(method, path, **kwargs)
        except ApiError as error:
            if error.status != 401:
                raise
            renew_tokens(self.base_url, self.state, force=True, clock=self.clock)
            if method.upper() == 'GET':
                # 읽기만 한 번 재요청한다. POST/PATCH/DELETE는 자동으로 반복하지 않는다.
                try:
                    return ApiClient(self.base_url, self.state['ui_tokens']['access_token']).request(method, path, **kwargs)
                except ApiError as second:
                    if second.status == 401:
                        end_session(self.state)
                        raise ApiError(401, 'AUTH_SESSION_ENDED', '사용자 인증을 다시 확인한다.') from second
                    raise
            raise ApiError(401, 'AUTH_RENEWED_REVIEW', '로그인은 갱신되었다. 쓰기 요청은 반복하지 않았다. 저장 결과를 확인한다.') from error

    def upload(self, path, **kwargs):
        renew_tokens(self.base_url, self.state, clock=self.clock)
        try:
            return ApiClient(self.base_url, self.state['ui_tokens']['access_token']).upload(path, **kwargs)
        except ApiError as error:
            if error.status == 401:
                renew_tokens(self.base_url, self.state, force=True, clock=self.clock)
                raise ApiError(401, 'AUTH_RENEWED_REVIEW', '인증을 갱신했다. 첨부 POST는 반복하지 않았다. 저장 결과를 확인한다.') from error
            raise

    def download(self, path, **kwargs):
        renew_tokens(self.base_url, self.state, clock=self.clock)
        try:
            return ApiClient(self.base_url, self.state['ui_tokens']['access_token']).download(path, **kwargs)
        except ApiError as error:
            if error.status != 401:
                raise
            renew_tokens(self.base_url, self.state, force=True, clock=self.clock)
            # 원본 읽기는 GET이다. 갱신 뒤 한 번만 재조회하며 업로드를 반복하지 않는다.
            try:
                return ApiClient(self.base_url, self.state['ui_tokens']['access_token']).download(path, **kwargs)
            except ApiError as second:
                if second.status == 401:
                    end_session(self.state)
                    raise ApiError(401, 'AUTH_SESSION_ENDED', '사용자 인증을 다시 확인한다.') from second
                raise

    def stream(self, path, **kwargs):
        renew_tokens(self.base_url, self.state, clock=self.clock)
        try:
            yield from ApiClient(self.base_url, self.state['ui_tokens']['access_token']).stream(path, **kwargs)
        except ApiError as error:
            if error.status == 401:
                renew_tokens(self.base_url, self.state, force=True, clock=self.clock)
                raise ApiError(401, 'AUTH_RENEWED_REVIEW', '인증을 갱신했다. AI POST는 반복하지 않았다. 원본을 먼저 확인한다.') from error
            raise
