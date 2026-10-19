"""답변별 평가 입력과 응답 미확정 UUID를 보관하고 명시적 저장·기록 조회를 처리한다."""

from dataclasses import dataclass
from uuid import uuid4

from clients.api_client import ApiError

# 한 답변의 평가 기록은 필요한 만큼 이어 읽는다.
FEEDBACK_PAGE_SIZE = 10


@dataclass
class FeedbackState:
    rating: int = 3
    comment: str = ""
    pending: dict | None = None
    records: list | None = None
    next_before_id: int | None = None
    notice: str = ""
    error: str = ""
    reset_form: bool = False


class FeedbackController:
    def __init__(self, api, answer_id: int, state: FeedbackState):
        self.api, self.state = api, state
        self.path = f"/answers/{answer_id}/feedback"

    def save(self, rating: int, comment: str) -> dict:
        state = self.state
        cleaned = comment.strip()
        if rating not in range(1, 6) or not cleaned or len(cleaned) > 1000:
            raise ApiError(422, "FEEDBACK_INPUT", "점수 1~5와 의견 1~1000자를 입력한다.")
        state.rating, state.comment = rating, cleaned
        body = {"rating": rating, "comment": cleaned}
        if state.pending is None:
            state.pending = {**body, "client_request_id": str(uuid4())}
        elif any(state.pending[key] != body[key] for key in body):
            raise ApiError(409, "PENDING_FEEDBACK_CHANGED", "이전 평가의 저장 결과를 먼저 확인한다. 같은 번호로 다른 평가를 보내지 않는다.")
        result = self.api.request("POST", self.path, body=state.pending)
        record = result["feedback"]
        records = {row["id"]: row for row in state.records or []}
        records[record["id"]] = record
        state.records = sorted(records.values(), key=lambda row: row["id"], reverse=True)
        state.pending, state.comment, state.error = None, "", ""
        state.reset_form = True
        state.notice = "평가 저장을 확인했다." + (" 기존 전송을 재사용했다." if result.get("replayed") else "")
        return result

    def load(self, *, older=False):
        # 이전 조회의 cursor를 유지해 페이지를 이어 읽는다.
        params = {"limit": FEEDBACK_PAGE_SIZE}
        if older and self.state.next_before_id is not None:
            params["before_id"] = self.state.next_before_id
        result = self.api.request("GET", self.path, params=params)
        records = {row["id"]: row for row in (self.state.records or []) if older}
        records.update({row["id"]: row for row in result["feedbacks"]})
        self.state.records = sorted(records.values(), key=lambda row: row["id"], reverse=True)
        self.state.next_before_id = result["next_before_id"]
        self.state.error = ""

    def finish_pending(self):
        # 화면의 미확정 시도만 끝낸다. 이미 서버에 저장된 평가를 삭제하지 않는다.
        self.state.pending = None
        self.state.error = ""
        self.state.notice = "이전 평가 시도를 종료했다. 내용을 바꾸면 새 전송 번호로 저장한다."
