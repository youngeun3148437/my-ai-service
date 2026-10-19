"""Supabase 로그인·갱신을 기존 함수에 맡기고 브라우저별 로그인 세션을 관리한다."""

from functools import lru_cache
from time import time
from urllib.parse import urlsplit

from anyio import fail_after
from cryptography.fernet import Fernet
from dotenv import find_dotenv
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import ServiceError, auth_result
from app.services import auth
from app.stores.login_sessions import LoginSessionStore


class LoginSessionSettings(BaseSettings):
    # 로그인 세션의 비밀 키와 절대 수명은 API 설정으로만 읽는다.
    model_config = SettingsConfigDict(env_file='.env', extra='ignore', hide_input_in_errors=True)
    login_sessions_enabled: bool = True
    login_session_encryption_key: SecretStr = SecretStr('')
    login_session_seconds: int = Field(default=604800, ge=60, le=604800)
    login_redirect_urls: list[str] = Field(default_factory=lambda: ['http://127.0.0.1:8501/'])
    login_cookie_secure: bool = True
    login_client_secret: SecretStr = SecretStr('')

    @model_validator(mode='after')
    def encryption_key(self):
        if self.login_sessions_enabled:
            try:
                Fernet(self.login_session_encryption_key.get_secret_value().encode())
            except (ValueError, TypeError):
                raise ValueError('LOGIN_SESSION_ENCRYPTION_KEY에 생성한 Fernet 키를 설정한다') from None
            if len(self.login_client_secret.get_secret_value()) < 32:
                raise ValueError('LOGIN_CLIENT_SECRET에 32자 이상의 서버 간 비밀값을 설정한다')
            if not self.login_redirect_urls:
                raise ValueError('LOGIN_REDIRECT_URLS에 화면의 정확한 기본 주소를 설정한다')
            for value in self.login_redirect_urls:
                try:
                    parts = urlsplit(value)
                    port = parts.port
                    valid = (parts.scheme in {'http', 'https'} and parts.hostname and port != 0
                             and parts.username is None and parts.password is None and parts.path == '/'
                             and '?' not in value and '#' not in value and '*' not in value and '\\' not in value
                             and not any(char in value for char in "';<>\"")
                             and not any(char.isspace() for char in value))
                    if not valid or (parts.scheme == 'http' and parts.hostname not in {'127.0.0.1', 'localhost', '::1'}):
                        raise ValueError()
                    if not self.login_cookie_secure and (parts.scheme != 'http' or parts.hostname not in {'127.0.0.1', 'localhost', '::1'}):
                        raise ValueError()
                except ValueError:
                    raise ValueError('LOGIN_REDIRECT_URLS에는 정확한 HTTPS 기본 주소 또는 로컬 기본 주소를 설정한다') from None
        return self


@lru_cache
def get_login_session_settings() -> LoginSessionSettings:
    # 일반 API 설정과 같은 상위 .env 탐색 규칙을 사용한다. 변경 후에는 서버를 재시작한다.
    return LoginSessionSettings(_env_file=find_dotenv(usecwd=True) or None)


class LoginSessionService:
    def __init__(self, store, client, seconds: int, clock=time):
        self.store, self.client, self.seconds, self.clock = store, client, seconds, clock

    def public(self, identifier: str, record: dict) -> dict:
        now = self.clock()
        remaining = int(record['session_until'] - now)
        lifetime = min(int(record['access_until'] - now), remaining)
        if lifetime <= 0 or remaining <= 0:
            raise ServiceError(401, 'LOGIN_SESSION_ENDED', '로그인 세션이 만료되었다')
        # frontend에는 갱신 토큰을 반환하지 않는다. 기존 API에는 이 접근 토큰으로 요청한다.
        return {'session_id': identifier, 'user_id': record['user_id'],
                'access_token': record['access_token'], 'expires_in': lifetime,
                'session_seconds': remaining}

    async def login(self, body) -> dict:
        try:
            with fail_after(5):
                result = await auth.login(self.client, body)
        except TimeoutError:
            raise ServiceError(503, 'AUTH_UNAVAILABLE', '로그인 서버를 확인하지 못했다. 다시 시도한다') from None
        now = self.clock()
        record = {**result, 'user_id': str(result['user_id']),
                  'access_until': now + result['expires_in'], 'session_until': now + self.seconds}
        identifier = await self.store.create(record)
        return self.public(identifier, record)

    async def restore(self, identifier: str, *, force=False, previous_access_token=None) -> dict:
        async with self.store.locked(identifier) as owner:
            raw = await self.store.read(identifier)
            phase, record = self.store.decode(raw)
            ttl = int(record['session_until'] - self.clock())
            if phase != 'ready' or ttl <= 0:
                # 응답 유실로 갱신 결과가 불확실하면 과거 refresh token을 다시 사용하지 않는다.
                await self.store.replace(identifier, owner, raw, '', 1)
                raise ServiceError(401, 'LOGIN_SESSION_ENDED', '인증 갱신 결과가 불확실하다. 다시 로그인한다')
            refresh_needed = record['access_until'] - self.clock() <= 30
            # 두 탭의 강제 갱신도 첫 탭이 이미 교체한 토큰이면 다시 갱신하지 않는다.
            refresh_needed |= force and previous_access_token == record['access_token']
            if refresh_needed:
                marked = self.store.encode(record, 'refreshing')
                await self.store.replace(identifier, owner, raw, marked, ttl)
                try:
                    with fail_after(5):
                        result = await auth.refresh(self.client, record['refresh_token'])
                    if str(result['user_id']) != record['user_id']:
                        raise ValueError('user changed')
                except (ServiceError, TimeoutError, ValueError):
                    # 이 세션은 더 복원하지 않는다. 자동 로그인·자동 갱신 재시도를 하지 않는다.
                    await self.store.replace(identifier, owner, marked, '', 1)
                    raise ServiceError(401, 'LOGIN_SESSION_ENDED', '인증 갱신을 확정하지 못했다. 다시 로그인한다') from None
                record.update(result)
                record['user_id'] = str(result['user_id'])
                record['access_until'] = self.clock() + result['expires_in']
                updated = self.store.encode(record)
                ttl = int(record['session_until'] - self.clock())
                if ttl <= 0:
                    await self.store.replace(identifier, owner, marked, '', 1)
                    raise ServiceError(401, 'LOGIN_SESSION_ENDED', '로그인 세션이 만료되었다')
                await self.store.replace(identifier, owner, marked, updated, ttl)
                raw = updated
            # 복원할 때 Supabase에서도 사용자를 확인한다. Redis만으로 사용자 신원을 확정하지 않는다.
            try:
                with fail_after(5):
                    result = await auth_result(self.client.auth.get_user(record['access_token']))
            except TimeoutError:
                raise ServiceError(503, 'AUTH_UNAVAILABLE', '사용자 인증을 확인하지 못했다') from None
            except ServiceError as error:
                if error.status_code in (401, 403):
                    await self.store.replace(identifier, owner, await self.store.read(identifier), '', 1)
                raise
            if result.user is None or str(result.user.id) != record['user_id']:
                await self.store.replace(identifier, owner, await self.store.read(identifier), '', 1)
                raise ServiceError(401, 'LOGIN_SESSION_ENDED', '사용자 인증을 다시 확인한다')
            # get_user를 기다리는 동안 다른 탭이 로그아웃했다면 복원 성공으로 반환하지 않는다.
            if await self.store.read(identifier) != raw:
                raise ServiceError(401, 'LOGIN_SESSION_ENDED', '로그인 세션이 변경되었다. 다시 로그인한다')
            return self.public(identifier, record)

    async def logout(self, identifier: str) -> dict:
        # 읽기와 삭제를 한 번에 실행한다. 갱신·사용자 확인 중인 탭이 있어도 즉시 폐기한다.
        raw = await self.store.revoke(identifier)
        if raw is None:
            return {'session_deleted': True, 'supabase_logout': 'not_found'}
        try:
            _, record = self.store.decode(raw)
        except ServiceError:
            return {'session_deleted': True, 'supabase_logout': 'unavailable'}
        try:
            with fail_after(5):
                await auth.logout(self.client, record['access_token'])
            status = 'confirmed'
        except (ServiceError, TimeoutError):
            status = 'unavailable'
        return {'session_deleted': True, 'supabase_logout': status}
