"""로컬 환경과 Cloud Secrets에서 FastAPI 주소를 선택하고 형식을 확인한다."""

from collections.abc import Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

LOCAL_API_BASE_URL = "http://127.0.0.1:8000"


class ApiConfigurationError(ValueError):
    """API 주소 설정의 오류다. 입력한 값이나 인증 정보는 오류에 넣지 않는다."""


def normalize_api_base_url(value: object) -> str:
    """서버의 기본 주소만 허용하고 양끝 공백·마지막 /를 정리한다."""
    if not isinstance(value, str) or not value.strip():
        raise ApiConfigurationError("API_BASE_URL에 FastAPI 서버 주소를 입력한다.")
    value = value.strip()
    if any(character.isspace() for character in value) or "\\" in value:
        raise ApiConfigurationError("API_BASE_URL의 공백과 역슬래시를 제거한다.")
    try:
        parts = urlsplit(value)
        # port를 읽을 때 숫자가 아닌 값이나 범위를 벗어난 값도 검사한다.
        port = parts.port
        if parts.scheme not in {"http", "https"} or not parts.hostname or port == 0:
            raise ValueError
        if parts.username is not None or parts.password is not None:
            raise ApiConfigurationError("API_BASE_URL에 계정·비밀번호를 넣지 않는다.")
    except ValueError as error:
        if isinstance(error, ApiConfigurationError):
            raise
        raise ApiConfigurationError("API_BASE_URL에 http:// 또는 https:// 서버 주소를 입력한다.") from None
    if "?" in value or "#" in value or parts.path not in {"", "/"}:
        raise ApiConfigurationError("API_BASE_URL에는 /docs·/health·쿼리 없이 서버 기본 주소만 넣는다.")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def resolve_api_base_url(environ: Mapping[str, str], read_secret: Callable[[], object]) -> str:
    """환경 변수 → Secrets → 로컬 기본값 순서다. 잘못된 설정은 기본값으로 숨기지 않는다."""
    # 값이 빈 문자열이어도 명시한 환경 변수를 우선 검사한다.
    if "API_BASE_URL" in environ:
        return normalize_api_base_url(environ["API_BASE_URL"])
    value = read_secret()
    if value is not None:
        return normalize_api_base_url(value)
    return LOCAL_API_BASE_URL
