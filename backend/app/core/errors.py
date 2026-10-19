"""외부 서비스 오류를 HTTP 오류로 바꾸고 인증 정보를 숨긴 공통 응답을 만든다."""

import logging
from collections.abc import Awaitable
from typing import TypeVar

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import RequestError
from postgrest import APIError
from starlette.exceptions import HTTPException as StarletteHTTPException
from supabase_auth.errors import AuthApiError, AuthError

logger = logging.getLogger("course.api")
T = TypeVar("T")
# T는 성공 결과의 자료형을 유지한다. Awaitable은 await로 결과를 받는 작업이다.


class ServiceError(HTTPException):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(
            status, {"code": code, "message": message},
            headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
        )


async def auth_result(operation: Awaitable[T], *, signup: bool = False) -> T:
    try:
        return await operation
    except AuthApiError as error:
        if error.status == 429:
            raise ServiceError(429, "AUTH_RATE_LIMIT", "요청이 많다. 잠시 뒤 다시 시도한다") from error
        if error.status >= 500:
            raise ServiceError(502, "AUTH_UNAVAILABLE", "Supabase Auth 요청에 실패했다") from error
        if error.code == "email_not_confirmed":
            raise ServiceError(403, "EMAIL_UNCONFIRMED", "이메일을 확인한 뒤 로그인한다") from error
        if signup:
            status = 409 if error.code in {"user_already_exists", "email_exists"} else 400
            raise ServiceError(status, "SIGNUP_REJECTED", "가입 조건과 이메일 발송 설정을 확인한다") from error
        raise ServiceError(401, "AUTH_REJECTED", "인증 정보를 확인한다") from error
    except (AuthError, RequestError) as error:
        raise ServiceError(502, "AUTH_UNAVAILABLE", "Supabase Auth 연결에 실패했다") from error


async def query_data(query):
    try:
        return (await query.execute()).data
    except RequestError as error:
        # 저장 시간 초과는 저장 실패의 확정 증거가 아니다. 원본 조회 후 재전송을 판단한다.
        raise ServiceError(502, "DATA_UNAVAILABLE", "Supabase 응답을 확인할 수 없다") from error
    except APIError as error:
        if error.code == 'PT404':
            raise ServiceError(404, 'NOT_FOUND', '대상 기록을 찾을 수 없다') from error
        if error.code == "PT409":
            raise ServiceError(409, "REQUEST_ID_REUSED", "같은 전송 번호에는 처음 보낸 글을 사용한다") from error
        if error.code == "PT410":
            raise ServiceError(410, "REQUEST_RESULT_REMOVED", "이 전송의 원본은 삭제되었다. 새 메시지로 자동 복원하지 않는다") from error
        if error.code in {"PGRST301", "PGRST303"}:
            raise ServiceError(401, "TOKEN_EXPIRED", "Access token을 확인한다") from error
        if error.code == "42501":
            raise ServiceError(403, "DATA_FORBIDDEN", "데이터 접근 권한을 확인한다") from error
        if error.code in {"23505", "23503", "23001"}:
            raise ServiceError(409, "DATA_CONFLICT", "중복 값 또는 연결할 기록을 확인한다") from error
        raise ServiceError(502, "DATA_UNAVAILABLE", "스키마·GRANT·RLS와 통합 SQL을 확인한다") from error


async def query_rows(query) -> list[dict]:
    return await query_data(query) or []


def first(rows: list[dict], resource: str) -> dict:
    if not rows:
        raise ServiceError(404, "NOT_FOUND", f"{resource} 기록을 찾을 수 없다")
    return rows[0]


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException):
        detail = error.detail if isinstance(error.detail, dict) else {
            "code": "HTTP_ERROR", "message": str(error.detail),
        }
        return JSONResponse(
            {"error": detail, "request_id": request.state.request_id},
            status_code=error.status_code, headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # input과 ctx에는 비밀번호·토큰 원문이 포함될 수 있어 응답에 넣지 않는다.
        fields = [{"location": list(e["loc"]), "type": e["type"]} for e in error.errors()]
        return JSONResponse(
            {"error": {"code": "VALIDATION_ERROR", "message": "요청 형식을 확인한다", "fields": fields},
             "request_id": request.state.request_id}, status_code=422,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        logger.error("unexpected_error request_id=%s type=%s", request.state.request_id, type(error).__name__)
        return JSONResponse(
            {"error": {"code": "INTERNAL_ERROR", "message": "서버 처리에 실패했다"},
             "request_id": request.state.request_id}, status_code=500,
        )
