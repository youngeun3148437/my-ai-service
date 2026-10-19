"""관리자 집계 응답에서 개인 대화 본문 없이 운영 지표와 로그 메타데이터를 보관한다."""
from datetime import datetime
from state.dashboard_state import DashboardState, SEOUL


class AdminDashboardController:
    def __init__(self, api, state: DashboardState):
        self.api, self.state = api, state

    def search(self, params: dict):
        result = self.api.request("GET", "/admin/dashboard", params={**params, "limit": 20})
        self.state.active_filters = dict(params)
        self.state.aggregate = {key: result[key] for key in ("summary", "daily", "feedback")}
        self.state.records = result["logs"]
        self.state.next_before_id = result["next_before_id"]
        self.state.error = ""
        self.state.last_synced_at = datetime.now(SEOUL)

    def more(self):
        if self.state.next_before_id is None or self.state.active_filters is None:
            return
        from clients.api_client import ApiError
        try:
            result = self.api.request("GET", "/admin/dashboard", params={
                **self.state.active_filters, "limit": 20, "before_id": self.state.next_before_id})
        except ApiError as error:
            if error.status in (401, 403):
                clear_admin(self.state)
            else:
                self.state.error = error.message
            raise
        records = {row["id"]: row for row in self.state.records + result["logs"]}
        self.state.records = sorted(records.values(), key=lambda row: row["id"], reverse=True)
        self.state.next_before_id = result["next_before_id"]

    def recover(self):
        # 자동 갱신에서도 사용자가 펼친 하단까지 다시 읽는다. 이전 페이지를 접어 버리지 않는다.
        from clients.api_client import ApiError
        if self.state.active_filters is None:
            return
        cutoff = min((row["id"] for row in self.state.records), default=None) if self.state.next_before_id is not None else None
        rows, cursor, seen = {}, None, set()
        aggregate = None
        while True:
            params = {**self.state.active_filters, "limit": 100}
            if cursor is not None:
                params["before_id"] = cursor
            result = self.api.request("GET", "/admin/dashboard", params=params)
            if aggregate is None:
                aggregate = {key: result[key] for key in ("summary", "daily", "feedback")}
            rows.update({row["id"]: row for row in result["logs"]})
            next_cursor = result["next_before_id"]
            if next_cursor is None or (cutoff is not None and any(row["id"] <= cutoff for row in result["logs"])):
                records = sorted((row for row in rows.values() if cutoff is None or row["id"] >= cutoff),
                                 key=lambda row: row["id"], reverse=True)
                more_available = next_cursor is not None or len(records) < len(rows)
                self.state.aggregate = aggregate
                self.state.records = records
                self.state.next_before_id = records[-1]["id"] if records and more_available else None
                self.state.error = ""
                self.state.last_synced_at = datetime.now(SEOUL)
                return
            if next_cursor in seen:
                raise ApiError(502, "LOG_CURSOR_INVALID", "운영 로그 페이지를 확인할 수 없다.")
            seen.add(next_cursor)
            cursor = next_cursor


def clear_admin(state):
    # 권한 거부에서 이전에 읽은 전체 사용자 지표도 즉시 없앤다.
    state.clear_results()
    state.error = "관리자 권한을 확인할 수 없다."
