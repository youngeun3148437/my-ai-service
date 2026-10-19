"""Streamlit 서버에서 FastAPI에 HTTP 요청을 보내고 안전한 오류로 변환한다."""

from dataclasses import dataclass

import httpx
from clients.stream_format import events


# dataclass가 아래 필드를 받는 __init__을 만든다. HTTP 오류를 UI에 전달할 정보 묶음이다.
@dataclass
class ApiError(Exception):
    status: int
    code: str
    message: str
    request_id: str = ""
    log_status: str | None = None
    attempt_id: str | None = None
    event_status: str | None = None


class ApiClient:
    def __init__(self, base_url: str, access_token: str | None = None):
        self.base_url, self.access_token = base_url.rstrip("/"), access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}

    @staticmethod
    def _json_response(response: httpx.Response) -> dict:
        try:
            data = response.json() if response.content else {}
        except ValueError as error:
            raise ApiError(response.status_code, "RESPONSE_ERROR", "JSON 응답 형식을 확인한다.") from error
        if response.is_error:
            detail = data.get("error", {}) if isinstance(data, dict) else {}
            if not isinstance(detail, dict):
                detail = {}
            # 비밀번호·토큰·원문 응답은 출력하지 않는다. 백엔드의 공통 오류 항목만 사용한다.
            raise ApiError(response.status_code, detail.get("code", "HTTP_ERROR"),
                           detail.get("message", "요청 결과를 확인한다."), data.get("request_id", "") if isinstance(data, dict) else "",
                           detail.get("log_status"), detail.get("attempt_id"), detail.get("event_status"))
        if not isinstance(data, dict):
            raise ApiError(response.status_code, "RESPONSE_ERROR", "객체 형태의 응답을 확인한다.")
        return data

    def request(self, method: str, path: str, *, body: dict | None = None,
                params: dict | None = None, timeout: float = 10.0) -> dict:
        # * 뒤의 인수는 body=, params=처럼 이름을 붙여 전달한다.
        # 현재 화면 세션의 토큰만 이 요청에 붙인다. 공유 객체의 헤더를 바꾸지 않는다.
        headers = self._headers()
        try:
            # Streamlit 코드는 Python 서버에서 실행된다. 브라우저의 fetch 요청이 아니다.
            # with가 끝나면 이 짧은 HTTP 작업의 연결을 정리한다.
            with httpx.Client(base_url=self.base_url, timeout=timeout) as client:
                # json은 요청 본문, params는 before_id 같은 쿼리 문자열로 전달된다.
                response = client.request(method, path, json=body, params=params, headers=headers)
        except httpx.RequestError as error:
            # 시간 초과는 저장 실패의 확정 증거가 아니다. 쓰기 요청을 자동 재전송하지 않는다.
            raise ApiError(0, "CONNECTION_ERROR", "FastAPI 응답을 확인할 수 없다. 주소·서버 실행과 저장 결과를 확인한다.") from error
        return self._json_response(response)

    def upload(self, path: str, *, filename: str, content_type: str,
               content: bytes, upload_id: str, timeout: float = 30.0) -> dict:
        try:
            with httpx.Client(base_url=self.base_url, timeout=timeout) as client:
                # files와 data를 함께 쓰면 multipart/form-data가 된다. JSON에 bytes를 넣지 않는다.
                response = client.post(path, files={"file": (filename, content, content_type)},
                                       data={"upload_id": upload_id}, headers=self._headers())
        except httpx.RequestError as error:
            raise ApiError(0, "UPLOAD_CONNECTION_ERROR", "첨부 저장 응답을 확인하지 못했다. 파일과 업로드 번호를 유지한다.") from error
        return self._json_response(response)

    def download(self, path: str, *, timeout: float = 30.0) -> bytes:
        try:
            with httpx.Client(base_url=self.base_url, timeout=timeout) as client:
                response = client.get(path, headers=self._headers())
        except httpx.RequestError as error:
            raise ApiError(0, "DOWNLOAD_CONNECTION_ERROR", "첨부 원본을 가져오지 못했다. 연결을 확인한다.") from error
        if response.is_error:
            self._json_response(response)
        # 인증된 응답은 이 사용자의 Session State에만 둔다. 전역 cache_data로 공유하지 않는다.
        return response.content

    def stream(self, path: str, *, body: dict, timeout: float = 130.0):
        headers = {'Accept': 'text/event-stream'}
        if self.access_token:
            headers['Authorization'] = f'Bearer {self.access_token}'
        try:
            # response.json()으로 전체를 기다리지 않는다. iter_lines가 끊겨 온 UTF-8 조각을 합친다.
            with httpx.Client(base_url=self.base_url, timeout=timeout) as client:
                with client.stream('POST', path, json=body, headers=headers) as response:
                    if response.is_error:
                        response.read()
                        try:
                            detail = response.json().get('error', {})
                        except (ValueError, AttributeError):
                            detail = {}
                        if not isinstance(detail, dict):
                            detail = {}
                        raise ApiError(response.status_code, detail.get('code', 'HTTP_ERROR'),
                                       detail.get('message', '스트림을 시작할 수 없다.'),
                                       log_status=detail.get('log_status'), attempt_id=detail.get('attempt_id'),
                                       event_status=detail.get('event_status'))
                    if 'text/event-stream' not in response.headers.get('content-type', ''):
                        raise ValueError('SSE content type required')
                    yield from events(response.iter_lines())
        except httpx.RequestError as error:
            raise ApiError(0, 'STREAM_CONNECTION_ERROR', '응답 연결이 끊겼다. 최근 원본에서 답변 저장 여부를 확인한다.') from error
        except (ValueError, TypeError) as error:
            raise ApiError(502, 'STREAM_RESPONSE_ERROR', '스트림 사건의 형식을 확인한다.') from error
