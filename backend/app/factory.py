"""FastAPI 앱에 라우터·CORS·공통 오류 처리·기본 진단 로그를 등록한다."""

import logging
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.core.errors import install_error_handlers
from app.core.resources import lifespan
from app.core.settings import Settings, get_settings
from app.routers import admin, answers, attachments, auth, browser_auth, conversations, login_sessions, messages, observability, realtime, users, work_sessions


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="AI 대화 서비스 API", version="2.0.0", lifespan=lifespan)
    app.state.settings_override = settings
    app.add_middleware(attachments.AttachmentBodyLimit)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["GET", "POST", "PATCH", "DELETE"],
                       allow_headers=["Authorization", "Content-Type"],
                       expose_headers=["X-Request-ID"])

    # 모든 요청의 앞뒤를 감싼다. 이 요청 번호는 중복 전송 방지 ID와 별개다.
    @app.middleware("http")
    async def request_metadata(request: Request, call_next):
        request.state.request_id = str(uuid4())
        started = perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        # 인증 응답은 오류도 캐시하지 않는다. 세션 ID·토큰은 URL이나 로그에 넣지 않는다.
        if request.url.path.startswith("/auth/") or request.url.path.startswith("/admin/") or request.url.path == "/me/role":
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        if request.url.path.startswith("/auth/"):
            # 인증 폼의 same-origin은 POST Origin을 유지한다. 외부 복귀는 no-referrer다.
            response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/auth/browser/"):
            response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            response.headers["X-Content-Type-Options"] = "nosniff"
        # 요청 본문·이메일·토큰·Redis 주소는 로그에 남기지 않는다.
        logging.getLogger("course.api").info(
            "request_id=%s method=%s status=%s duration_ms=%.1f",
            request.state.request_id, request.method, response.status_code,
            (perf_counter() - started) * 1000,
        )
        return response

    install_error_handlers(app)
    # 기능별 APIRouter를 하나의 FastAPI 서버에 등록한다.
    for router in (auth.router, browser_auth.router, login_sessions.router, users.router, conversations.router, messages.router, work_sessions.router, answers.router, attachments.router, observability.router, realtime.router, admin.router):
        app.include_router(router)

    @app.get("/health", tags=["서버"])
    async def health():
        return {"status": "ok"}  # 실행 상태만 확인한다. 외부 서비스·RLS 검증은 아니다.

    return app
