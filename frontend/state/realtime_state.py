"""사용자별 작업 수명·큐 사건·원본 재조회 필요 여부를 UI 실행에서만 관리한다."""

from dataclasses import dataclass

from clients.realtime_client import RealtimeConfig, SSEWorker


@dataclass
class RealtimeState:
    enabled: bool = False
    worker: SSEWorker | None = None
    status: str = "연결 안 됨"
    error: str = ""
    dirty: bool = False
    auth_expired: bool = False
    blocked: bool = False
    connected: bool = False
    last_sync: float = 0.0
    retry_after: float = 0.0

    def stop(self):
        if self.worker is not None:
            self.worker.stop()
        self.worker = None
        self.enabled, self.auth_expired, self.blocked = False, False, False
        self.connected = False
        self.status = "연결 안 됨"

    def consume(self, owner_id: str, access_token: str):
        worker = self.worker
        if worker is None:
            return
        if worker.config.owner_id != owner_id or worker.config.access_token != access_token:
            worker.stop()
            self.worker = None
            self.connected = False
            self.dirty = True
            self.status = "인증 갱신 후 재연결"
            return
        items, overflow = worker.drain()
        self.dirty = self.dirty or overflow
        if overflow:
            self.status = "알림이 몰려 원본 재조회 필요"
        for item in items:
            if item["owner_id"] != owner_id or item["generation"] != worker.generation:
                continue
            name, data = item["name"], item["data"]
            if name == "ready":
                # ready 뒤 원본 재조회를 예약하고 연결 상태를 바꾼다.
                self.status, self.error, self.dirty = "연결됨", "", True
                self.connected = True
            elif name == "log.saved":
                # event_id/log_id를 새로운 전체 목록으로 취급하지 않는다. DB가 원본이다.
                self.dirty = True
            elif name == "reconnecting":
                self.status, self.dirty = "재연결 중", True
                self.connected = False
            elif name in ("auth", "error"):
                self.error = f"{data.get('code', 'REALTIME_ERROR')}: {data.get('message', '연결을 확인한다.')}"
                self.status, self.dirty = "원본 재조회 필요", True
                self.connected = False
                self.auth_expired = name == "auth" or data.get("code") == "AUTH_EXPIRED"
                self.blocked = bool(data.get("blocked", False))
            elif name == "lease_expired":
                self.status, self.dirty = "다시 연결하는 중", True
                self.connected = False
            elif name == "heartbeat":
                self.connected = True
                if not self.dirty:
                    self.status = "연결됨"

    def ensure(self, config: RealtimeConfig, *, factory=None, source=None):
        if not self.enabled or self.blocked or self.auth_expired:
            return
        if self.worker is None or not self.worker.alive():
            if self.worker is not None:
                self.worker.stop()
            self.worker = (factory or SSEWorker)(config, source=source).start()
            self.status = "연결 준비 중"
        self.worker.touch()

    def needs_refresh(self, now: float) -> bool:
        # 발행 실패·누락은 Pub/Sub 재생으로 해결할 수 없다. 연결 중에도 30초마다 DB를 확인한다.
        return self.enabled and now >= self.retry_after and (self.dirty or now - self.last_sync >= 30.0)

    def synchronized(self, now: float):
        self.dirty, self.last_sync, self.retry_after = False, now, 0.0
        if self.connected:
            self.status, self.error = "연결됨", ""
