"""한국 날짜를 UTC 조회 범위로 바꾸고 전체 집계와 cursor 목록을 따로 보관한다."""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from clients.api_client import ApiError

SEOUL = ZoneInfo("Asia/Seoul")


def local_today() -> date:
    return datetime.now(SEOUL).date()


def query_filters(start: date, end: date, mode: str, status: str) -> dict:
    if not isinstance(start, date) or not isinstance(end, date) or not 1 <= (end - start).days + 1 <= 31:
        raise ApiError(422, "DASHBOARD_PERIOD", "시작일과 종료일을 순서대로 정하고 최대 31일을 조회한다.")
    if mode not in ("all", "normal", "stream") or status not in ("all", "succeeded", "failed", "cancelled"):
        raise ApiError(422, "DASHBOARD_FILTER", "생성 방식과 처리 결과를 확인한다.")
    # 한국 날짜의 시작은 포함하고 종료일 다음 날의 시작은 제외한다. UTC offset을 함께 보낸다.
    since = datetime.combine(start, time.min, SEOUL).astimezone(timezone.utc)
    until = datetime.combine(end + timedelta(days=1), time.min, SEOUL).astimezone(timezone.utc)
    params = {"since": since.isoformat(), "until": until.isoformat()}
    if mode != "all":
        params["mode"] = mode
    if status != "all":
        params["status"] = status
    return params


@dataclass
class DashboardState:
    start: date = field(default_factory=lambda: local_today() - timedelta(days=6))
    end: date = field(default_factory=local_today)
    mode: str = "all"
    status: str = "all"
    active_filters: dict | None = None
    aggregate: dict | None = None
    records: list = field(default_factory=list)
    next_before_id: int | None = None
    error: str = ""
    last_synced_at: datetime | None = None

    def clear_results(self):
        self.active_filters, self.aggregate, self.next_before_id = None, None, None
        self.records = []
        self.last_synced_at = None


class DashboardController:
    def __init__(self, api, state: DashboardState):
        self.api, self.state = api, state

    def _error(self, error: ApiError):
        self.state.error = f"{error.code}: {error.message}"
        if error.status in (401, 403):
            self.state.clear_results()

    def search(self, params: dict):
        try:
            aggregate = self.api.request("GET", "/dashboard", params=params)
            page = self.api.request("GET", "/ai-request-logs", params={**params, "limit": 20})
        except ApiError as error:
            self._error(error)
            raise
        # 두 읽기가 성공한 뒤 한 번에 교체한다. 실패 시 다른 기간의 집계와 목록을 섞지 않는다.
        self.state.active_filters = dict(params)
        self.state.aggregate = aggregate
        self.state.records = page["logs"]
        self.state.next_before_id = page["next_before_id"]
        self.state.error = ""
        self.state.last_synced_at = datetime.now(SEOUL)

    def more(self):
        if self.state.active_filters is None or self.state.next_before_id is None:
            return
        params = {**self.state.active_filters, "limit": 20, "before_id": self.state.next_before_id}
        try:
            page = self.api.request("GET", "/ai-request-logs", params=params)
        except ApiError as error:
            self._error(error)
            raise
        records = {row["id"]: row for row in self.state.records + page["logs"]}
        self.state.records = sorted(records.values(), key=lambda row: row["id"], reverse=True)
        self.state.next_before_id = page["next_before_id"]
        self.state.error = ""

    def recover(self):
        if self.state.active_filters is None:
            return
        # ID는 트랜잭션 commit 순서가 아니다. after_id 증가분 조회로 늦게 저장된 행을 건너뛰지 않는다.
        # 일부 페이지를 불러왔으면 기존 하단 ID까지, 전부 불러왔으면 끝까지 최신부터 재조회한다.
        cutoff = min((row["id"] for row in self.state.records), default=None) if self.state.next_before_id is not None else None
        params = dict(self.state.active_filters)
        records, cursor, seen = {}, None, set()
        try:
            aggregate = self.api.request("GET", "/dashboard", params=params)
            while True:
                page_params = {**params, "limit": 100}
                if cursor is not None:
                    page_params["before_id"] = cursor
                page = self.api.request("GET", "/ai-request-logs", params=page_params)
                rows = page["logs"]
                records.update({row["id"]: row for row in rows})
                next_cursor = page["next_before_id"]
                reached = cutoff is not None and any(row["id"] <= cutoff for row in rows)
                if reached or next_cursor is None:
                    selected = [row for row in records.values() if cutoff is None or row["id"] >= cutoff]
                    selected.sort(key=lambda row: row["id"], reverse=True)
                    more_available = next_cursor is not None or len(selected) < len(records)
                    cursor = selected[-1]["id"] if selected and more_available else None
                    break
                if next_cursor in seen:
                    raise ApiError(502, "LOG_CURSOR_INVALID", "로그 페이지 경계를 확인한다.")
                seen.add(next_cursor)
                cursor = next_cursor
        except ApiError as error:
            self._error(error)
            raise
        self.state.aggregate, self.state.records = aggregate, selected
        self.state.next_before_id, self.state.error = cursor, ""
        self.state.last_synced_at = datetime.now(SEOUL)
