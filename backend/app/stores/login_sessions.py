"""로그인 세션의 암호화된 토큰을 Redis에 보관하고 같은 세션의 갱신을 직렬화한다."""

import asyncio
import hashlib
import json
import re
import secrets
from contextlib import asynccontextmanager
from time import monotonic, time

from anyio import fail_after, move_on_after
from cryptography.fernet import Fernet, InvalidToken
from redis.exceptions import RedisError

from app.core.errors import ServiceError

# 쿠키에는 32바이트 난수만 들어간다. Redis 키에는 난수의 SHA-256 해시를 사용한다.
SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
UPDATE = """
if redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
if redis.call('GET', KEYS[1]) ~= ARGV[2] then return 0 end
if ARGV[3] == '' then redis.call('DEL', KEYS[1]); return 1 end
redis.call('SET', KEYS[1], ARGV[3], 'EX', ARGV[4]); return 1
"""
RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
REVOKE = """
local previous = redis.call('GET', KEYS[1])
redis.call('DEL', KEYS[1])
return previous
"""


def session_keys(identifier: str) -> tuple[str, str]:
    if not SESSION_PATTERN.fullmatch(identifier):
        raise ServiceError(401, 'LOGIN_SESSION_ENDED', '로그인 세션을 다시 시작한다')
    digest = hashlib.sha256(identifier.encode()).hexdigest()
    # 중괄호 안의 같은 값은 Redis Cluster에서도 두 키를 같은 슬롯에 둔다.
    # v2는 HttpOnly 경로로 만든 세션만 복원한다. 예전 JavaScript 쿠키의 세션과 분리한다.
    prefix = f'course:login:v2:{{{digest}}}'
    return prefix + ':record', prefix + ':lock'


class LoginSessionStore:
    def __init__(self, redis, encryption_key: str, timeout: float):
        self.redis, self.cipher, self.timeout = redis, Fernet(encryption_key.encode()), timeout

    async def operation(self, awaitable):
        try:
            with fail_after(self.timeout):
                return await awaitable
        except (RedisError, TimeoutError) as error:
            raise ServiceError(503, 'LOGIN_SESSION_UNAVAILABLE', '로그인 세션 저장소를 확인할 수 없다') from error

    def encode(self, record: dict, phase='ready') -> str:
        # 갱신 토큰은 Redis에도 평문으로 쓰지 않는다. 암호화 키는 backend 환경 변수에 둔다.
        secret = self.cipher.encrypt(json.dumps(record).encode()).decode()
        return json.dumps({'phase': phase, 'secret': secret}, separators=(',', ':'))

    def decode(self, raw: str) -> tuple[str, dict]:
        try:
            wrapper = json.loads(raw)
            record = json.loads(self.cipher.decrypt(wrapper['secret'].encode()))
            if (not isinstance(record, dict) or not all(record.get(k) for k in
                    ('user_id', 'access_token', 'refresh_token', 'access_until', 'session_until'))):
                raise ValueError()
            return wrapper['phase'], record
        except (ValueError, KeyError, TypeError, InvalidToken, AttributeError) as error:
            raise ServiceError(401, 'LOGIN_SESSION_ENDED', '로그인 세션을 다시 시작한다') from error

    async def create(self, record: dict) -> str:
        # NX는 이미 있는 키를 덮어쓰지 않는다. 토큰의 수명과 로그인 세션의 수명은 별개다.
        identifier = secrets.token_urlsafe(32)
        key, _ = session_keys(identifier)
        ttl = max(1, int(record['session_until'] - time()))
        if not await self.operation(self.redis.set(key, self.encode(record), ex=ttl, nx=True)):
            raise ServiceError(503, 'LOGIN_SESSION_UNAVAILABLE', '로그인 세션을 준비하지 못했다')
        return identifier

    async def read(self, identifier: str) -> str:
        key, _ = session_keys(identifier)
        raw = await self.operation(self.redis.get(key))
        if not raw:
            raise ServiceError(401, 'LOGIN_SESSION_ENDED', '로그인 세션이 만료되었다. 다시 로그인한다')
        return raw

    async def replace(self, identifier: str, owner: str, previous: str, value: str, ttl: int):
        key, lock = session_keys(identifier)
        result = await self.operation(self.redis.eval(UPDATE, 2, key, lock, owner, previous, value, max(1, ttl)))
        if not result:
            if await self.operation(self.redis.get(key)) is None:
                raise ServiceError(401, 'LOGIN_SESSION_ENDED', '로그인 세션이 종료되었다. 다시 로그인한다')
            raise ServiceError(503, 'LOGIN_SESSION_CHANGED', '로그인 세션 변경 결과를 다시 확인한다')

    async def revoke(self, identifier: str) -> str | None:
        # 로그아웃은 갱신 잠금을 기다리지 않는다. 진행 중인 갱신도 이전 값 비교에서 쓰기를 거절한다.
        key, _ = session_keys(identifier)
        return await self.operation(self.redis.eval(REVOKE, 1, key))

    @asynccontextmanager
    async def locked(self, identifier: str):
        _, lock = session_keys(identifier)
        owner, deadline = secrets.token_hex(16), monotonic() + 1.0
        while not await self.operation(self.redis.set(lock, owner, nx=True, px=20000)):
            if monotonic() >= deadline:
                raise ServiceError(503, 'LOGIN_SESSION_BUSY', '다른 화면이 인증을 확인하고 있다. 잠시 뒤 다시 확인한다')
            await asyncio.sleep(0.05)
        try:
            yield owner
        finally:
            # 취소되어도 자신이 획득한 잠금만 해제한다. 갱신 요청은 별도 5초 제한을 둔다.
            with move_on_after(2, shield=True):
                try:
                    await self.redis.eval(RELEASE, 1, lock, owner)
                except RedisError:
                    pass
