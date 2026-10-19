"""일반 계정의 개인 기록과 관리자의 서비스 운영 조회를 분리한다."""

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.dependencies import Authenticated
from app.routers.observability import Filters
from app.schemas import AIRequestLog, Dashboard, LogFilters
from app.stores.admin import AdminDatabase

router = APIRouter(tags=['계정 권한·서비스 운영'])


class AppRole(BaseModel):
    role: Literal['user', 'admin']


class AdminDashboard(Dashboard):
    # 일반 대시보드와 같은 집계 형식에 제한된 로그 메타데이터 페이지를 붙인다.
    logs: list[AIRequestLog]
    next_before_id: int | None


async def get_admin_database(context: Authenticated) -> AdminDatabase:
    db = AdminDatabase(context)
    await db.require_admin()
    return db


Admin = Annotated[AdminDatabase, Depends(get_admin_database)]


@router.get('/me/role', response_model=AppRole)
async def my_role(context: Authenticated):
    return AppRole(role=await AdminDatabase(context).role())


@router.get('/admin/dashboard', response_model=AdminDashboard)
async def dashboard(db: Admin, filters: Filters,
                    before_id: Annotated[int | None, Query(gt=0)] = None,
                    limit: Annotated[int, Query(ge=1, le=100)] = 20):
    # 관리자 조회도 기간을 제한한다. 시작/끝 중 하나만 고르면 나머지는 7일 범위로 채운다.
    until = filters.until or (filters.since + timedelta(days=7) if filters.since else datetime.now(timezone.utc))
    since = filters.since or until - timedelta(days=7)
    bounded = LogFilters(since=since, until=until, status=filters.status, mode=filters.mode)
    return await db.dashboard(bounded, before_id, limit)
