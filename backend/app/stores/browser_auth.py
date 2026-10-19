"""브라우저 인증 흐름과 일회용 코드를 짧게 보관한다. 값은 Redis에서도 암호화한다."""

import hashlib
import json
import secrets

from cryptography.fernet import InvalidToken

from app.core.errors import ServiceError
from app.stores.login_sessions import SESSION_PATTERN

CONSUME = """
local value = redis.call('GET', KEYS[1])
redis.call('DEL', KEYS[1])
return value
"""


class BrowserAuthStore:
    def __init__(self, sessions):
        self.sessions = sessions

    def key(self, kind: str, identifier: str) -> str:
        if not SESSION_PATTERN.fullmatch(identifier):
            raise ServiceError(400, 'LOGIN_FLOW_ENDED', '인증 화면을 다시 연다')
        digest = hashlib.sha256(identifier.encode()).hexdigest()
        return f'course:login:v2:{kind}:{digest}'

    async def issue(self, kind: str, values: dict, seconds: int = 60) -> str:
        identifier = secrets.token_urlsafe(32)
        encrypted = self.sessions.cipher.encrypt(json.dumps(values).encode()).decode()
        saved = await self.sessions.operation(self.sessions.redis.set(self.key(kind, identifier), encrypted,
                                                                      ex=seconds, nx=True))
        if not saved:
            raise ServiceError(503, 'LOGIN_FLOW_UNAVAILABLE', '인증 화면을 다시 연다')
        return identifier

    async def read(self, kind: str, identifier: str, *, consume=False) -> dict:
        key = self.key(kind, identifier)
        # 교환·로그아웃의 최종 POST에서만 GET+DEL을 원자적으로 수행한다.
        raw = await self.sessions.operation(self.sessions.redis.eval(CONSUME, 1, key) if consume
                                            else self.sessions.redis.get(key))
        if not raw:
            raise ServiceError(400, 'LOGIN_FLOW_ENDED', '인증 요청이 만료되었거나 이미 사용되었다')
        try:
            result = json.loads(self.sessions.cipher.decrypt(raw.encode()))
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, TypeError, InvalidToken, AttributeError):
            raise ServiceError(400, 'LOGIN_FLOW_ENDED', '인증 화면을 다시 연다') from None
