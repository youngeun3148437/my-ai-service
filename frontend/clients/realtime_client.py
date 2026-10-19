"""고정 신원으로 SSE를 읽고 bounded Queue에만 전달하는 백그라운드 작업이다."""

from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4

import httpx

from clients.api_client import ApiClient, ApiError
from clients.stream_format import events


@dataclass(frozen=True)
class RealtimeConfig:
    base_url: str
    owner_id: str
    access_token: str = field(repr=False)
    queue_size: int = 64
    lease_seconds: float = 20.0
    retry_seconds: float = 2.0


class SSEWorker:
    def __init__(self, config: RealtimeConfig, *, source=None, clock=None):
        self.config, self.source, self.clock = config, source, clock or monotonic
        self.generation = str(uuid4())
        self.queue = Queue(maxsize=config.queue_size)
        self.stopped, self.lock = Event(), Lock()
        self.overflowed = False
        self.last_touch = self.clock()
        self.response = None
        self.completed = Event()
        self.thread = Thread(target=self._run, name="course-realtime-reader", daemon=True)
        self.watchdog = Thread(target=self._watch_lease, name="course-realtime-lease", daemon=True)

    def start(self):
        self.thread.start()
        self.watchdog.start()
        return self

    def touch(self):
        # UI만 호출한다. 탭이 사라져 pulse가 끊기면 별도 lease 감시가 연결을 닫는다.
        with self.lock:
            self.last_touch = self.clock()

    def active(self):
        with self.lock:
            leased = self.clock() - self.last_touch < self.config.lease_seconds
        return leased and not self.stopped.is_set()

    def stop(self):
        self.stopped.set()
        self._close_response()
        # HTTP 수신이 풀릴 때까지 UI를 오래 막지 않는다.
        if self.thread.is_alive():
            self.thread.join(timeout=0.2)

    def _close_response(self):
        with self.lock:
            response = self.response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    def _watch_lease(self):
        # 긴 수신 대기에도 탭 종료를 감지한다. 이 작업도 Session State를 읽지 않는다.
        while not self.completed.wait(0.5):
            if self.stopped.is_set():
                return
            if not self.active():
                self.push("lease_expired", {"message": "화면 확인이 멈춰 알림 연결을 정리했다."})
                self.stopped.set()
                self._close_response()
                return

    def alive(self):
        return self.thread.is_alive()

    def push(self, name: str, data: dict):
        item = {"name": name, "data": data, "owner_id": self.config.owner_id, "generation": self.generation}
        try:
            self.queue.put_nowait(item)
        except Full:
            # 버린 사건을 개별 재생하지 않는다. UI에서 현재 DB 범위를 다시 조회하게 한다.
            with self.lock:
                self.overflowed = True

    def drain(self):
        result = []
        while True:
            try:
                result.append(self.queue.get_nowait())
            except Empty:
                break
        with self.lock:
            overflow, self.overflowed = self.overflowed, False
        return result, overflow

    def _http_events(self):
        # 서버가 허용하는 heartbeat 최대 30초보다 길게 기다린다. lease 감시는 별도다.
        timeout = httpx.Timeout(35.0, connect=3.0)
        with httpx.Client(base_url=self.config.base_url, timeout=timeout) as client:
            with client.stream("GET", "/ai-request-events", headers={
                "Authorization": f"Bearer {self.config.access_token}", "Accept": "text/event-stream",
            }) as response:
                with self.lock:
                    self.response = response
                try:
                    if not self.active():
                        return
                    if response.is_error:
                        response.read()
                        ApiClient._json_response(response)
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        raise ApiError(502, "REALTIME_RESPONSE_ERROR", "실시간 응답 형식을 확인한다.")
                    yield from events(response.iter_lines())
                finally:
                    with self.lock:
                        self.response = None

    def _run(self):
        try:
            self._run_loop()
        finally:
            self.completed.set()

    def _run_loop(self):
        # 이 메서드는 Streamlit·SessionState·사용자 토큰 갱신 함수를 호출하지 않는다.
        while self.active():
            try:
                reader = self.source(self.config, self.stopped) if self.source else self._http_events()
                try:
                    for name, data in reader:
                        if not self.active():
                            break
                        if name not in ("ready", "log.saved", "heartbeat", "error"):
                            raise ApiError(502, "REALTIME_RESPONSE_ERROR", "알 수 없는 알림 사건이다.")
                        self.push(name, data)
                        if name == "error":
                            if data.get("code") == "AUTH_EXPIRED":
                                return
                            break
                finally:
                    if hasattr(reader, "close"):
                        reader.close()
                if self.active():
                    self.push("reconnecting", {"message": "연결을 다시 준비한다."})
            except ApiError as error:
                if self.stopped.is_set():
                    break
                self.push("auth" if error.status == 401 else "error", {
                    "code": error.code, "message": error.message,
                    "blocked": error.status in (403, 404, 422, 503),
                })
                if error.status in (401, 403, 404, 422, 503):
                    return
            except (httpx.RequestError, ValueError, TypeError):
                if not self.stopped.is_set():
                    self.push("error", {"code": "REALTIME_CONNECTION_ERROR", "message": "연결이 끊겼다. 원본 범위를 다시 확인한다."})
            except Exception:
                if not self.stopped.is_set():
                    self.push("error", {"code": "REALTIME_READER_ERROR", "message": "알림 연결을 다시 확인한다.", "blocked": True})
                return
            if self.stopped.wait(self.config.retry_seconds):
                break
        if not self.stopped.is_set():
            self.push("lease_expired", {"message": "화면 확인이 멈춰 알림 연결을 정리했다."})
