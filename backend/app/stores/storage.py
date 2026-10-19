"""공유 HTTP 풀과 사용자 JWT로 비공개 Storage 파일을 업로드·다운로드·삭제한다."""

from urllib.parse import quote

import httpx

from app.core.errors import ServiceError
from app.core.settings import Settings

BUCKET = 'course-attachments'
MAX_BYTES = 5 * 1024 * 1024


class StorageGateway:
    def __init__(self, http: httpx.AsyncClient, settings: Settings, access_token: str):
        self.http, self.settings, self.access_token = http, settings, access_token

    def headers(self) -> dict:
        # apikey는 프로젝트 식별·접근용이며, Authorization의 사용자 JWT가 RLS의 신원이다.
        return {'apikey': self.settings.supabase_publishable_key,
                'Authorization': f'Bearer {self.access_token}'}

    def url(self, path: str = '') -> str:
        base = str(self.settings.supabase_url).rstrip('/') + '/storage/v1/object/' + BUCKET
        return base + ('/' + quote(path, safe='/') if path else '')

    @staticmethod
    def check(response: httpx.Response) -> None:
        if not response.is_error:
            return
        # Storage는 HTTP 400에 JSON statusCode 404/409를 함께 넣을 수도 있다.
        code = None
        try:
            payload = response.json()
            code = str(payload.get('statusCode', ''))
            if payload.get('error') in {'Duplicate', 'Asset Already Exists'}:
                code = '409'
        except (ValueError, AttributeError):
            pass
        if response.status_code == 404 or code == '404':
            raise ServiceError(404, 'ATTACHMENT_CONTENT_NOT_FOUND', 'Storage 파일이 없다. 첨부 상태를 확인한다')
        if response.status_code == 409 or code == '409':
            raise ServiceError(409, 'STORAGE_OBJECT_EXISTS', '같은 경로의 파일이 있다. 원본을 확인한다')
        if response.status_code == 401 or code == '401':
            raise ServiceError(401, 'TOKEN_EXPIRED', 'Access token을 확인한다')
        if response.status_code == 403 or code == '403':
            raise ServiceError(403, 'STORAGE_FORBIDDEN', 'Storage RLS 정책과 사용자 경로를 확인한다')
        raise ServiceError(502, 'STORAGE_UNAVAILABLE', 'Storage 응답을 확인할 수 없다. 자동 재전송하지 않는다')

    async def upload(self, path: str, data: bytes, content_type: str) -> None:
        try:
            response = await self.http.post(self.url(path), content=data,
                headers={**self.headers(), 'Content-Type': content_type, 'x-upsert': 'false'},
                timeout=self.settings.http_timeout_seconds)
        except httpx.RequestError as error:
            # 시간 초과로 실제 저장 여부를 확정할 수 없다. pending 기록을 남긴다.
            raise ServiceError(502, 'STORAGE_UNCERTAIN', '업로드 결과가 불확실하다. 같은 파일과 업로드 번호로 직접 재시도한다') from error
        self.check(response)

    async def download(self, path: str) -> bytes:
        try:
            # stream은 응답을 제한 크기씩 읽고 이 응답만 닫는다. 공유 풀은 닫지 않는다.
            async with self.http.stream('GET', self.url(path), headers=self.headers(),
                                        timeout=self.settings.http_timeout_seconds) as response:
                if response.is_error:
                    await response.aread()
                    self.check(response)
                data = bytearray()
                async for chunk in response.aiter_bytes(64 * 1024):
                    if len(data) + len(chunk) > MAX_BYTES:
                        raise ServiceError(502, 'STORAGE_CONTENT_INVALID', '저장된 파일의 크기가 제한을 넘는다')
                    data.extend(chunk)
                return bytes(data)
        except httpx.RequestError as error:
            raise ServiceError(502, 'STORAGE_UNAVAILABLE', 'Storage 파일을 읽을 수 없다') from error

    async def delete(self, path: str) -> None:
        try:
            response = await self.http.request('DELETE', self.url(), headers=self.headers(),
                json={'prefixes': [path]}, timeout=self.settings.http_timeout_seconds)
        except httpx.RequestError as error:
            raise ServiceError(502, 'STORAGE_DELETE_UNCERTAIN', '파일 삭제 결과가 불확실하다. 첨부 기록을 유지하고 직접 다시 확인한다') from error
        self.check(response)
        # 이미 없는 파일의 삭제는 빈 배열로 성공할 수 있다. DB 삭제 정책도 실제 부재를 검사한다.
