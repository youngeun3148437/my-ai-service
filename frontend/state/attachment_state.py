"""대화별 파일·업로드 번호·선택 첨부를 보관하고 명시적 저장·원본 조회를 처리한다."""

from dataclasses import dataclass, field
from pathlib import PurePath
from uuid import uuid4

from clients.api_client import ApiError

MAX_FILE_BYTES = 5 * 1024 * 1024
FILE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf"}


@dataclass
class AttachmentState:
    records: list | None = None
    chosen: dict | None = None
    pending: dict | None = None
    selected_ids: list = field(default_factory=list)
    selection_version: int = 0
    contents: dict = field(default_factory=dict)
    uploader_version: int = 0
    notice: str = ""
    error: str = ""


def chosen_file(filename: str, content: bytes) -> dict:
    suffix = PurePath(filename).suffix.lower()
    if suffix not in FILE_TYPES:
        raise ApiError(415, "ATTACHMENT_TYPE", "PNG·JPEG·PDF 파일을 선택한다.")
    if not content:
        raise ApiError(422, "ATTACHMENT_EMPTY", "내용이 있는 파일을 선택한다.")
    if len(content) > MAX_FILE_BYTES:
        raise ApiError(413, "ATTACHMENT_TOO_LARGE", "파일 크기는 5 MiB 이하여야 한다.")
    return {"filename": filename, "content_type": FILE_TYPES[suffix], "content": content}


class AttachmentController:
    def __init__(self, api, conversation_id: int, state: AttachmentState):
        self.api, self.conversation_id, self.state = api, conversation_id, state
        self.path = f"/conversations/{conversation_id}/attachments"

    def select_file(self, filename: str, content: bytes):
        selected = chosen_file(filename, content)
        if self.state.pending and any(self.state.pending[k] != selected[k] for k in selected):
            raise ApiError(409, "PENDING_UPLOAD_CHANGED", "이전 파일의 저장 결과를 먼저 확인한다. 같은 업로드 번호로 다른 파일을 보내지 않는다.")
        self.state.chosen = selected
        # 파일 선택 성공은 전체 첨부 목록의 조회 성공이 아니다.
        if self.state.records is not None:
            self.state.error = ""

    def save(self) -> dict:
        state = self.state
        if state.pending is None:
            if state.chosen is None:
                raise ApiError(422, "ATTACHMENT_REQUIRED", "파일을 선택한다.")
            # 파일 bytes도 함께 고정한다. 응답 유실 뒤에는 같은 번호·파일로만 명시 재시도한다.
            state.pending = {**state.chosen, "upload_id": str(uuid4())}
        result = self.api.upload(self.path, **state.pending)
        if result.get("status") != "ready":
            raise ApiError(503, "ATTACHMENT_NOT_READY", "첨부가 아직 pending 상태다. 원본 목록을 확인하고 같은 파일로 다시 저장한다.")
        self._remember(result)
        if result["id"] not in state.selected_ids and len(state.selected_ids) < 2:
            state.selected_ids.append(result["id"])
        state.pending, state.chosen = None, None
        state.uploader_version += 1
        # 업로드 한 건의 성공으로 실패한 collection 조회를 완료 처리하지 않는다.
        state.error = "" if state.records is not None else "첨부 목록을 확인하지 못했다. 첨부 다시 불러오기로 원본 목록을 확인한다."
        state.notice = "첨부를 저장했다. 질문에 사용할 파일을 아래에서 확인한다."
        return result

    def _remember(self, record: dict):
        # collection을 아직 읽지 않았다면 업로드 응답 한 건으로 전체 목록을 대체하지 않는다.
        if self.state.records is None:
            return
        else:
            records = {row["id"]: row for row in self.state.records}
            records[record["id"]] = record
            self.state.records = list(records.values())

    def refresh(self):
        result = self.api.request("GET", self.path)
        records = result["attachments"]
        self.state.records = records
        ready_ids = {row["id"] for row in records if row["status"] == "ready"}
        selected = [identifier for identifier in self.state.selected_ids if identifier in ready_ids]
        if selected != self.state.selected_ids:
            self.state.selected_ids = selected
        # 다른 화면에서 삭제된 파일 bytes도 현재 화면에서 제거한다.
        all_ids = {row["id"] for row in records}
        self.state.contents = {identifier: data for identifier, data in self.state.contents.items() if identifier in all_ids}
        self.state.error = ""

    def read(self, attachment_id: str):
        content = self.api.download(f"{self.path}/{attachment_id}/content")
        self.state.contents[attachment_id] = content
        return content

    def delete(self, attachment_id: str):
        result = self.api.request("DELETE", f"{self.path}/{attachment_id}")
        if not result.get("deleted"):
            raise ApiError(502, "ATTACHMENT_DELETE_RESPONSE", "첨부 삭제 결과를 원본 목록에서 확인한다.")
        self.state.records = [row for row in self.state.records or [] if row["id"] != attachment_id]
        self.state.contents.pop(attachment_id, None)
        self.state.selected_ids = [identifier for identifier in self.state.selected_ids if identifier != attachment_id]
        self.state.notice = "첨부를 삭제했다. 다른 화면에서 선택한 파일은 원본 목록을 다시 확인한다."

    def finish_pending(self):
        # 서버 파일 삭제와 별개로 이 화면의 업로드 작업만 끝낸다. 원본 확인 후 버튼으로 호출한다.
        self.state.pending, self.state.chosen = None, None
        self.state.uploader_version += 1
        self.state.error = ""
        self.state.notice = "화면의 업로드 작업을 종료했다. 저장된 파일은 원본 목록에서 확인한다."
