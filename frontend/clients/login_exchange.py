"""한 번만 쓰는 코드의 교환 결과를 화면 재실행과 분리해 보관한다."""

from concurrent.futures import Future
from threading import Lock, Thread
from time import monotonic

from clients.api_client import ApiError


class LoginExchange:
    def __init__(self, receipt: dict):
        # 생성 시에는 요청하지 않는다. 호출자가 먼저 사용자별 Session State에 넣는다.
        self.receipt = dict(receipt)
        self.callback_state = receipt['state']
        self.completed_at = None
        self._future = Future()
        self._lock = Lock()
        self._started = False

    def start(self, client, body: dict):
        # 이전 실행과 새 실행이 같은 작업을 만나도 실제 HTTP 교환은 한 번만 시작한다.
        with self._lock:
            if self._started:
                return
            self._started = True
            try:
                Thread(target=self._exchange, args=(client, dict(body)), daemon=True).start()
            except Exception:
                self._fail()

    def _fail(self, error=None):
        self.completed_at = monotonic()
        self._future.set_exception(error or ApiError(
            503, 'AUTH_EXCHANGE_UNAVAILABLE', '로그인 결과를 확인하지 못했다. 다시 로그인한다.'))

    def _exchange(self, client, body):
        # 이 작업 스레드는 Streamlit API·Session State·secrets를 읽거나 바꾸지 않는다.
        # 화면 실행이 중단돼도 응답은 Future에 남아 다음 실행이 같은 결과를 받는다.
        try:
            result = client.request('POST', '/auth/browser-sessions/exchange', body=body)
        except ApiError as error:
            self._fail(error)
        except Exception:
            self._fail()
        else:
            self.completed_at = monotonic()
            self._future.set_result(result)

    def result(self):
        # HTTP 클라이언트의 제한 시간 안에서 기다린다. 다시 읽어도 코드를 재전송하지 않는다.
        return self._future.result()
