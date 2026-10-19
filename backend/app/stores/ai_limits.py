"""Redis에서 사용자별 AI 시간창 한도와 동시 생성 한 개를 원자적으로 접수한다."""

from dataclasses import dataclass
from uuid import uuid4

from anyio import move_on_after
from redis import RedisError

from app.core.errors import ServiceError

# 같은 계정의 키는 Redis Cluster에서도 같은 슬롯에 둔다. 1=접수, 0=생성 중, -1=한도다.
ACQUIRE = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
local count = tonumber(redis.call('GET', KEYS[2]) or '0')
if count >= tonumber(ARGV[2]) then return -1 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[4])
count = redis.call('INCR', KEYS[2])
if count == 1 then redis.call('EXPIRE', KEYS[2], ARGV[3]) end
return 1
"""
RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


@dataclass
class GenerationLease:
    redis: object
    key: str
    token: str
    released: bool = False

    async def release(self):
        # 취소 중에도 짧게 정리한다. 이전 요청이 새 요청의 잠금을 삭제하지 않도록 토큰을 비교한다.
        if self.released:
            return
        self.released = True
        with move_on_after(2, shield=True):
            try:
                await self.redis.eval(RELEASE, 1, self.key, self.token)
            except RedisError:
                pass  # 정리에 실패해도 TTL 뒤 잠금이 만료된다. 답변 저장 결과를 뒤집지 않는다.


class AILimits:
    def __init__(self, redis, user_id: str, settings):
        self.redis, self.user_id, self.settings = redis, user_id, settings

    async def acquire(self) -> GenerationLease | None:
        if not self.settings.ai_limits_enabled:
            return None
        # 모델의 전체 시간 제한과 DB/캐시/로그 정리 예산보다 길게 잡는다.
        lease_seconds = int(self.settings.ai_timeout_seconds + 3 * self.settings.http_timeout_seconds
                            + self.settings.log_write_timeout_seconds + 20) + 1
        key = f'course:release:ai:{{{self.user_id}}}:active'
        budget_key = f'course:release:ai:{{{self.user_id}}}:window'
        token = str(uuid4())
        lease = GenerationLease(self.redis, key, token)
        try:
            result = await self.redis.eval(ACQUIRE, 2, key, budget_key, token,
                self.settings.ai_request_limit, self.settings.ai_request_window_seconds, lease_seconds)
        except RedisError as error:
            # EVAL 응답을 못 받아도 접수되었을 수 있다. 같은 토큰의 잠금만 짧게 정리한다.
            await lease.release()
            # 제한 장치가 끊겼을 때 유료 모델을 무제한 호출하지 않는다. 질문 원본은 이미 남아 있다.
            raise ServiceError(503, 'AI_LIMIT_UNAVAILABLE', 'AI 사용량을 확인할 수 없다. 잠시 뒤 답변을 다시 요청한다') from error
        except BaseException:
            await lease.release()
            raise
        if result == 0:
            raise ServiceError(409, 'AI_GENERATION_BUSY', '이 계정의 다른 답변을 생성 중이다. 완료 후 다시 요청한다')
        if result == -1:
            raise ServiceError(429, 'AI_USAGE_LIMIT', '잠시 동안 보낼 수 있는 AI 요청 수를 넘었다. 잠시 뒤 다시 요청한다')
        if result != 1:
            await lease.release()
            raise ServiceError(503, 'AI_LIMIT_UNAVAILABLE', 'AI 사용량을 확인할 수 없다')
        return lease
