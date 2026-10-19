"""SSE의 사건 이름·JSON 본문·빈 줄 경계를 만들고 외부 JSON 조각을 읽는다."""
import json
from collections.abc import AsyncIterator

from anyio import fail_after, move_on_after
from fastapi.responses import StreamingResponse
from starlette.types import Send

from app.core.errors import ServiceError


class ManagedStreamingResponse(StreamingResponse):
    # 이 응답은 aclose가 있는 서비스의 비동기 제너레이터에 사용한다.
    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            # 브라우저 전달 중 종료되어도 제너레이터를 닫아 안쪽 모델 연결까지 정리한다.
            with move_on_after(5, shield=True):
                await self.body_iterator.aclose()


class TimedEventResponse(ManagedStreamingResponse):
    def __init__(self, content, status_code: int = 200, *args,
                 send_timeout: float = 5, close_callback=None, **kwargs):
        # FastAPI는 이 기본 상태 코드를 읽어 OpenAPI 응답을 만든다. 런타임 응답에도 전달한다.
        super().__init__(content, status_code, *args, **kwargs)
        self.send_timeout = send_timeout
        self.close_callback = close_callback

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # 시작 헤더를 보내기 전에 연결이 끊기면 제너레이터의 finally는 실행되지 않을 수 있다.
            # 응답 자체에도 정리를 연결해 준비된 구독이 남지 않도록 한다.
            if self.close_callback is not None:
                with move_on_after(3, shield=True):
                    await self.close_callback()

    async def stream_response(self, send: Send) -> None:
        async def bounded_send(message):
            # 느린 수신자 때문에 yield 다음의 정리가 멈추지 않도록 전달 시간도 제한한다.
            try:
                with fail_after(self.send_timeout):
                    await send(message)
            except TimeoutError as error:
                raise OSError('event delivery timed out') from error
        await super().stream_response(bounded_send)


def sse_event(name: str, data: dict) -> str:
    # SSE(Server-Sent Events)는 UTF-8 텍스트다. 빈 줄이 사건 하나의 끝이다.
    return f'event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'


async def json_events(lines: AsyncIterator[str]) -> AsyncIterator[dict]:
    parts = []
    async for line in lines:
        if not line:
            if parts:
                value = json.loads('\n'.join(parts))
                if not isinstance(value, dict):
                    raise ValueError('JSON object required')
                yield value
                parts.clear()
        elif line.startswith('data:'):
            parts.append(line[5:].lstrip(' '))
    # 마지막 빈 줄까지 와야 완전한 사건이다. 잘린 사건은 정상 완료로 처리하지 않는다.
    if parts:
        raise ValueError('unfinished SSE event')


def stream_error_event(error: ServiceError, metadata: dict) -> str:
    # 스트림을 시작한 뒤의 ServiceError와 종료 정보를 받아 error 사건을 만든다.
    # HTTP 상태는 이미 200이다. status/code/message는 SSE data 안에서 전달한다.
    # 시작한 HTTP 응답 안에서 실패 상태·안전한 오류·로그 정보를 error 사건으로 전달한다.
    return sse_event('error', {'status': error.status_code, **error.detail, **metadata})
