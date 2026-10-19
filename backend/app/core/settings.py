"""기존 환경 변수에서 서비스 설정을 읽고 자료형·범위를 검사한다."""

from functools import lru_cache

from dotenv import find_dotenv
from pydantic import Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # BaseSettings는 환경 변수 이름을 필드 이름에 연결하고 자료형을 검증한다.
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    supabase_url: HttpUrl
    supabase_publishable_key: str = Field(min_length=1, repr=False)
    redis_url: str = Field(min_length=1, repr=False)
    work_session_ttl_seconds: int = Field(default=1800, ge=1, le=86400)
    cache_ttl_seconds: int = Field(default=60, ge=1, le=3600)
    # 배포본은 동봉된 SQL을 적용한 환경에서 모든 서비스 기능을 사용한다.
    cache_version_enabled: bool = True
    message_page_size: int = Field(default=30, ge=1, le=100)
    http_timeout_seconds: float = Field(default=10, gt=0, le=60)
    redis_timeout_seconds: float = Field(default=2, gt=0, le=30)
    http_max_connections: int = Field(default=50, ge=1, le=500)
    redis_max_connections: int = Field(default=20, ge=1, le=200)
    cors_origins: list[str] = Field(default_factory=list)
    # AI 키는 FastAPI 환경에서만 읽는다. UI가 보낸 키·모델 이름을 사용하지 않는다.
    gemini_api_key: str = Field(default="", repr=False)
    gemini_model: str = Field(default="gemini-3.5-flash-lite", pattern=r"^gemini-[a-z0-9.-]+$")
    ai_timeout_seconds: float = Field(default=45, gt=0, le=120)
    ai_max_output_tokens: int = Field(default=1000, ge=100, le=2000)
    ai_context_limit: int = Field(default=20, ge=1, le=50)
    attachments_enabled: bool = True
    observability_enabled: bool = True
    log_write_timeout_seconds: float = Field(default=2, gt=0, le=5)
    realtime_enabled: bool = True
    realtime_connection_seconds: float = Field(default=60, gt=0, le=60)
    realtime_heartbeat_seconds: float = Field(default=10, gt=0, le=30)
    realtime_send_timeout_seconds: float = Field(default=5, gt=0, le=10)
    # 같은 계정의 여러 탭·서버 인스턴스도 Redis의 한도와 생성 잠금을 공유한다.
    ai_limits_enabled: bool = True
    ai_request_limit: int = Field(default=20, ge=1, le=1000)
    ai_request_window_seconds: int = Field(default=60, ge=10, le=3600)

    @field_validator("redis_url")
    @classmethod
    def redis_address(cls, value: str) -> str:
        if not value.startswith(("redis://", "rediss://")):
            raise ValueError("Redis TCP 연결 주소를 설정한다")
        return value


# lru_cache는 설정 객체를 재사용한다. 사용자별 토큰을 이 캐시에 넣지는 않는다.
@lru_cache
def get_settings() -> Settings:
    # 기존 uv 프로젝트의 .env도 찾는다. 요청마다 파일을 다시 읽지 않는다.
    return Settings(_env_file=find_dotenv(usecwd=True) or None)
