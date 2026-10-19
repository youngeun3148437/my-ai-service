"""인증 세션 요청에만 서버 간 비밀 헤더를 붙인다. 브라우저에는 보내지 않는다."""

from clients.api_client import ApiClient, ApiError
from config.login_config import login_client_secret


class LoginSessionClient(ApiClient):
    def __init__(self, base_url: str, access_token: str | None = None, *, client_secret: str | None = None):
        super().__init__(base_url, access_token)
        self.client_secret = client_secret

    def _headers(self) -> dict:
        # 별도 교환 작업은 화면 실행에서 미리 읽은 비밀값을 사용한다.
        secret = self.client_secret if self.client_secret is not None else login_client_secret()
        return {**super()._headers(), 'X-Login-Client-Secret': secret}

    def request(self, method: str, path: str, **kwargs) -> dict:
        # 같은 비밀을 일반 데이터 API나 외부 주소로 전송하지 않는다.
        allowed = {'/auth/browser-sessions/exchange', '/auth/browser-sessions/restore',
                   '/auth/browser-sessions/logout-ticket'}
        if method != 'POST' or path not in allowed:
            raise ApiError(400, 'LOGIN_REQUEST_ERROR', '로그인 세션 요청 경로를 확인한다.')
        return super().request(method, path, **kwargs)
