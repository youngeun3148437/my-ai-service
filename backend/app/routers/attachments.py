"""multipart 파일 요청과 보호된 첨부 조회·다운로드·삭제 경로를 연결한다."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from fastapi.responses import JSONResponse

from app.core.dependencies import Authenticated, SharedResources
from app.core.errors import ServiceError
from app.schemas import AttachmentDeleted, AttachmentPage, AttachmentRecord, PositiveId
from app.services.attachments import AttachmentService, read_upload
from app.stores.database import Database
from app.stores.storage import MAX_BYTES, StorageGateway

router = APIRouter(tags=['첨부 파일'])


def get_attachment_service(context: Authenticated, resources: SharedResources) -> AttachmentService:
    if not resources.settings.attachments_enabled:
        raise ServiceError(503, 'ATTACHMENTS_UNAVAILABLE', '첨부 SQL 적용 후 ATTACHMENTS_ENABLED=true로 설정하고 서버를 재시작한다')
    return AttachmentService(Database(context, attachments_enabled=True),
        StorageGateway(resources.http, resources.settings, context.access_token))


Attachments = Annotated[AttachmentService, Depends(get_attachment_service)]


@router.post('/conversations/{conversation_id}/attachments', response_model=AttachmentRecord,
             status_code=201, responses={200: {'model': AttachmentRecord}})
async def upload_attachment(conversation_id: PositiveId, response: Response, service: Attachments,
                            file: Annotated[UploadFile, File()], upload_id: Annotated[UUID, Form()]):
    filename, content_type = file.filename or '', file.content_type or ''
    data = await read_upload(file)
    record, created = await service.upload(conversation_id, upload_id, filename, content_type, data)
    response.status_code = 201 if created else 200
    return record


@router.get('/conversations/{conversation_id}/attachments', response_model=AttachmentPage)
async def attachments(conversation_id: PositiveId, service: Attachments):
    return await service.list_attachments(conversation_id)


@router.get('/conversations/{conversation_id}/attachments/{attachment_id}/content', response_class=Response)
async def attachment_content(conversation_id: PositiveId, attachment_id: UUID, service: Attachments):
    record, data = await service.content(conversation_id, attachment_id)
    return Response(data, media_type=record.content_type,
                    headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@router.delete('/conversations/{conversation_id}/attachments/{attachment_id}', response_model=AttachmentDeleted)
async def delete_attachment(conversation_id: PositiveId, attachment_id: UUID, service: Attachments):
    await service.delete(conversation_id, attachment_id)
    return AttachmentDeleted()


class AttachmentBodyLimit:
    """폼 파서가 큰 파일을 임시 디스크에 쓰기 전에 HTTP 본문 크기를 제한한다."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if not (scope['type'] == 'http' and scope.get('method') == 'POST'
                and path.startswith('/conversations/') and path.endswith('/attachments')):
            return await self.app(scope, receive, send)
        # 최대 파일 5 MiB와 작은 파일명·UUID·multipart 경계의 여유분만 허용한다.
        body = bytearray()
        limit = MAX_BYTES + 64 * 1024
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            if len(body) + len(chunk) > limit:
                error = JSONResponse({'error': {'code': 'ATTACHMENT_TOO_LARGE',
                    'message': '파일은 5 MiB 이하여야 한다'},
                    'request_id': scope.get('state', {}).get('request_id', '')}, status_code=413)
                return await error(scope, receive, send)
            body.extend(chunk)
            if not message.get('more_body', False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        await self.app(scope, bounded_receive, send)
