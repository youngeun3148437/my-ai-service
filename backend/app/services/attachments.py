"""파일의 실제 형식을 검사하고 pending → Storage 저장 → ready 순서를 관리한다."""

import base64
import hashlib
import warnings
from io import BytesIO
from pathlib import PurePosixPath
from uuid import UUID

from fastapi import UploadFile
from PIL import Image
from pypdf import PdfReader
from starlette.concurrency import run_in_threadpool

from app.core.errors import ServiceError
from app.schemas import AttachmentPage, AttachmentRecord
from app.stores.database import Database
from app.stores.storage import MAX_BYTES, StorageGateway

EXTENSIONS = {'image/png': '.png', 'image/jpeg': '.jpg', 'application/pdf': '.pdf'}
MAX_PIXELS = 20_000_000
MAX_PDF_PAGES = 20


def validate_file(filename: str, content_type: str, data: bytes) -> str:
    """확장자·MIME 선언뿐 아니라 디코딩 가능한 실제 파일을 검사한다."""
    if not filename or len(filename) > 180 or not filename.isprintable() or '/' in filename or '\\' in filename:
        raise ServiceError(422, 'ATTACHMENT_FILENAME', '경로 없이 180자 이하의 파일명을 사용한다')
    suffix = PurePosixPath(filename).suffix.lower()
    expected = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.pdf': 'application/pdf'}
    if content_type not in EXTENSIONS or expected.get(suffix) != content_type:
        raise ServiceError(415, 'ATTACHMENT_TYPE', 'PNG·JPEG·PDF의 확장자와 Content-Type을 확인한다')
    if not data:
        raise ServiceError(422, 'ATTACHMENT_EMPTY', '빈 파일을 첨부할 수 없다')
    if len(data) > MAX_BYTES:
        raise ServiceError(413, 'ATTACHMENT_TOO_LARGE', '파일은 5 MiB 이하여야 한다')
    try:
        if content_type == 'application/pdf':
            if not data.startswith(b'%PDF-'):
                raise ValueError('PDF header required')
            pdf = PdfReader(BytesIO(data), strict=True)
            if pdf.is_encrypted:
                raise ServiceError(422, 'PDF_ENCRYPTED', '암호화되지 않은 PDF를 사용한다')
            if not 1 <= len(pdf.pages) <= MAX_PDF_PAGES:
                raise ServiceError(422, 'PDF_PAGE_LIMIT', 'PDF는 1~20페이지로 준비한다')
        else:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as image:
                    if image.format != ('PNG' if content_type == 'image/png' else 'JPEG'):
                        raise ValueError('decoded format mismatch')
                    if image.width * image.height > MAX_PIXELS:
                        raise ServiceError(422, 'IMAGE_PIXEL_LIMIT', '이미지는 2천만 픽셀 이하여야 한다')
                    image.verify()
                # verify는 구조를 검사한다. 다시 열어 픽셀도 끝까지 디코딩한다.
                with Image.open(BytesIO(data)) as image:
                    image.load()
    except ServiceError:
        raise
    except Exception as error:
        # 외부 라이브러리의 파서 오류 원문을 응답에 넣지 않는다.
        raise ServiceError(422, 'ATTACHMENT_INVALID', '파일을 정상적으로 읽을 수 없다. 실제 형식을 확인한다') from error
    return hashlib.sha256(data).hexdigest()


async def read_upload(file: UploadFile) -> bytes:
    # read()에 크기를 지정해 전체 파일을 제한 없이 메모리로 읽지 않는다.
    data = bytearray()
    try:
        while chunk := await file.read(64 * 1024):
            if len(data) + len(chunk) > MAX_BYTES:
                raise ServiceError(413, 'ATTACHMENT_TOO_LARGE', '파일은 5 MiB 이하여야 한다')
            data.extend(chunk)
        return bytes(data)
    finally:
        await file.close()


class AttachmentService:
    def __init__(self, db: Database, storage: StorageGateway):
        self.db, self.storage = db, storage

    def check_record(self, row: dict, conversation_id: int) -> AttachmentRecord:
        record = AttachmentRecord.model_validate(row)
        expected = f'{self.db.context.auth_user_id}/{conversation_id}/{record.id}{EXTENSIONS[record.content_type]}'
        if record.conversation_id != conversation_id or record.auth_user_id != self.db.context.auth_user_id or record.object_path != expected:
            raise ServiceError(404, 'ATTACHMENT_NOT_FOUND', '이 대화에서 첨부를 찾을 수 없다')
        return record

    async def record(self, conversation_id: int, attachment_id: UUID) -> AttachmentRecord:
        row = await self.db.attachment(conversation_id, attachment_id)
        if row is None:
            raise ServiceError(404, 'ATTACHMENT_NOT_FOUND', '이 대화에서 첨부를 찾을 수 없다')
        return self.check_record(row, conversation_id)

    async def list_attachments(self, conversation_id: int) -> AttachmentPage:
        # 대화 ID로 DB 메타데이터 목록을 조회한다. check_record로 소유자·대화·경로를 검증한다.
        # 응답의 pending은 접수 상태, ready는 원본 저장이 확정된 상태다. 파일 바이트는 포함하지 않는다.
        # 대화 권한을 확인하고 첨부 목록 응답을 만든다.
        await self.db.conversation(conversation_id)
        rows = await self.db.attachments(conversation_id)
        return AttachmentPage(attachments=[self.check_record(row, conversation_id) for row in rows])

    async def upload(self, conversation_id: int, upload_id: UUID, filename: str, content_type: str, data: bytes) -> tuple[AttachmentRecord, bool]:
        # multipart에서 읽은 이름·MIME·바이트와 업로드 UUID를 받는다. 이 저장 절차는 제공된다.
        # DB pending → 비공개 Storage 원본 → DB ready 순서다. 실패 시 같은 UUID·파일로 직접 재시도한다.
        await self.db.conversation(conversation_id)
        # 파일 파싱은 CPU 작업이다. 이벤트 루프를 막지 않도록 작업 스레드에서 검사한다.
        digest = await run_in_threadpool(validate_file, filename, content_type, data)
        row = await self.db.attachment(conversation_id, upload_id)
        created = row is None
        if created:
            values = {'id': str(upload_id), 'conversation_id': conversation_id,
                'auth_user_id': str(self.db.context.auth_user_id), 'filename': filename,
                'content_type': content_type, 'byte_size': len(data), 'sha256': digest, 'status': 'pending',
                'object_path': f'{self.db.context.auth_user_id}/{conversation_id}/{upload_id}{EXTENSIONS[content_type]}'}
            try:
                row = await self.db.create_attachment(values)
            except ServiceError as error:
                if error.status_code != 409:
                    raise
                # 동시에 같은 UUID를 접수했다면 다시 읽어 본문을 비교한다. INSERT를 반복하지 않는다.
                row = await self.db.attachment(conversation_id, upload_id)
                if row is None:
                    raise
                created = False
        record = self.check_record(row, conversation_id)
        if (record.filename, record.content_type, record.byte_size, record.sha256) != (filename, content_type, len(data), digest):
            raise ServiceError(409, 'UPLOAD_ID_REUSED', '같은 업로드 번호에는 처음 보낸 파일명과 파일 내용을 사용한다')
        if record.status == 'ready':
            return record, False
        existing = None
        if not created:
            try:
                existing = await self.storage.download(record.object_path)
            except ServiceError as error:
                if error.status_code != 404:
                    raise
        if existing is None:
            # upsert=False다. 동시 업로드가 충돌하면 다음 명시적 재시도에서 원본을 확인한다.
            await self.storage.upload(record.object_path, data, content_type)
        elif len(existing) != record.byte_size or hashlib.sha256(existing).hexdigest() != digest:
            raise ServiceError(409, 'STORAGE_CONTENT_CONFLICT', '저장된 파일이 접수 내용과 다르다. 첨부를 삭제한 뒤 새로 업로드한다')
        row = await self.db.ready_attachment(conversation_id, upload_id)
        return self.check_record(row, conversation_id), created

    async def content(self, conversation_id: int, attachment_id: UUID) -> tuple[AttachmentRecord, bytes]:
        # 제공된 조회·검사로 본인 대화의 ready 첨부만 통과시킨다. pending은 다운로드할 수 없다.
        await self.db.conversation(conversation_id)
        record = await self.record(conversation_id, attachment_id)
        if record.status != 'ready':
            raise ServiceError(409, 'ATTACHMENT_PENDING', '업로드가 아직 확정되지 않았다. 같은 파일로 직접 재시도하거나 삭제한다')
        # Storage 경로로 받은 바이트를 DB의 byte_size·sha256과 대조하고 (메타데이터, 바이트)를 반환한다.
        # ready 파일의 원본 바이트를 읽고 크기·내용을 대조한다.
        data = await self.storage.download(record.object_path)
        if len(data) != record.byte_size or hashlib.sha256(data).hexdigest() != record.sha256:
            raise ServiceError(502, 'STORAGE_CONTENT_INVALID', '저장된 파일이 첨부 기록과 일치하지 않는다')
        return record, data

    async def delete(self, conversation_id: int, attachment_id: UUID) -> None:
        # Storage 원본 삭제 성공 후 DB 첨부 정보를 삭제한다.
        await self.db.conversation(conversation_id)
        record = await self.record(conversation_id, attachment_id)
        # 파일 삭제가 실패하면 메타데이터를 남긴다. 다음 명시적 삭제는 없는 파일도 처리한다.
        await self.storage.delete(record.object_path)
        await self.db.delete_attachment(conversation_id, attachment_id)

    async def ai_parts(self, conversation_id: int, attachment_ids: list[UUID]) -> list[dict]:
        # 선택한 ID만 content로 읽고 실제 형식을 다시 검사한다. 지난 턴의 파일은 자동으로 붙이지 않는다.
        # MIME과 Base64 바이트를 Gemini inlineData 목록으로 반환한다. URL·사용자 토큰을 보내지 않는다.
        # 확인한 파일 MIME·Base64를 현재 질문의 모델 입력으로 만든다.
        parts = []
        for attachment_id in attachment_ids:
            record, data = await self.content(conversation_id, attachment_id)
            await run_in_threadpool(validate_file, record.filename, record.content_type, data)
            # base64는 바이트를 JSON에 담는 표현이다. 비공개 URL·사용자 JWT는 Gemini에 보내지 않는다.
            parts.append({'inlineData': {'mimeType': record.content_type,
                                        'data': base64.b64encode(data).decode('ascii')}})
        return parts
