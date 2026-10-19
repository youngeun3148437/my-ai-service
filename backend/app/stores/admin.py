"""검증된 사용자 JWT로 앱 역할과 제한된 운영 집계 RPC만 호출한다."""

from app.core.dependencies import UserContext
from app.core.errors import ServiceError, query_data


class AdminDatabase:
    def __init__(self, context: UserContext):
        self.client = context.client

    async def role(self) -> str:
        # JWT의 user_metadata나 브라우저가 보낸 role을 믿지 않고 DB의 현재 부여 내역을 읽는다.
        role = await query_data(self.client.rpc('current_app_role', {}))
        if role not in {'user', 'admin'}:
            raise ServiceError(502, 'ROLE_UNAVAILABLE', '계정 권한을 확인할 수 없다')
        return role

    async def require_admin(self) -> None:
        if await self.role() != 'admin':
            raise ServiceError(403, 'ADMIN_REQUIRED', '서비스 운영 권한이 필요하다')

    async def dashboard(self, filters, before_id: int | None, limit: int) -> dict:
        # SQL 함수도 호출 순간의 역할을 다시 검사한다. 개인 데이터 테이블의 RLS는 바꾸지 않는다.
        return await query_data(self.client.rpc('admin_ai_dashboard', {
            'p_since': filters.since.isoformat(), 'p_until': filters.until.isoformat(),
            'p_status': filters.status, 'p_mode': filters.mode,
            'p_before_id': before_id, 'p_limit': limit,
        }))
