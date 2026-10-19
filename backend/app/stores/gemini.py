"""공유 HTTP 연결로 Gemini에 대화 내용을 전달하고 응답 본문·사용량을 꺼낸다."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from time import monotonic

import httpx
from anyio import fail_after, move_on_after

from app.core.errors import ServiceError
from app.core.settings import Settings
from app.schemas import TokenUsage
from app.streaming import json_events

def example_instruction() -> str:
    # 예시 답변에 쉬운 설명·구체적 예시 하나·길이 제한을 요청하는 서버 지시다.
    return "쉬운 한국어 설명과 구체적인 예시 하나를 포함한다. 답변은 900자 이내로 작성한다."


STYLES = {
    "brief": "핵심을 짧고 쉬운 한국어로 설명한다. 답변은 600자 이내로 작성한다.",
    "example": "쉬운 한국어 설명과 구체적인 예시 하나를 포함한다. 답변은 900자 이내로 작성한다.",
}


@dataclass(frozen=True)
class GeneratedAnswer:
    # dataclass는 글·사용량을 묶고, frozen은 생성 뒤 필드 재대입을 막는다.
    text: str
    usage: TokenUsage


@dataclass(frozen=True)
class GenerationEvent:
    # delta는 생성 중 글 조각, answer는 검증이 끝난 완성 결과다.
    delta: str = ''
    answer: GeneratedAnswer | None = None


def generation_body(messages: list[dict], style: str, max_tokens: int,
                    attachment_parts: list[dict] | None = None) -> dict:
    # DB 글을 Gemini의 문맥으로 변환한다. assistant는 모델 본문의 model 역할이다.
    contents = [{"role": "model" if m["role"] == "assistant" else "user",
                 "parts": [{"text": m["content"]}]} for m in messages]
    # 파일 연결은 준비된 문맥의 마지막 현재 질문에만 적용한다. 파일 없는 첫 연결은 빈 목록이다.
    if contents:
        parts = attachment_parts if attachment_parts is not None else messages[-1].get('attachment_parts', [])
        contents[-1]['parts'].extend(parts)
    # brief는 기존 지시를 쓴다. example을 선택한 경우에만 예시 지시 함수를 호출한다.
    instruction = "학습을 돕는 조교다. " + (example_instruction() if style == "example" else STYLES[style])
    # 준비된 지시·문맥·출력 한도를 세 JSON 키로 반환한다.
    return {
        "systemInstruction": {"parts": [{"text": instruction}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens},
    }


def parse_answer(payload: dict) -> GeneratedAnswer:
    try:
        candidate = payload["candidates"][0]
        if candidate.get("finishReason") == "MAX_TOKENS":
            # 출력 한도로 잘린 글을 완성 답변으로 저장하지 않는다.
            raise ServiceError(502, "AI_OUTPUT_LIMIT", "AI 출력 한도에 도달했다. 답변 지시와 출력 토큰 설정을 확인한다")
        # STOP은 정상 종료다. 누락·차단·알 수 없는 종료의 글은 완성 답변으로 저장하지 않는다.
        if candidate.get("finishReason") != "STOP":
            raise ValueError("generation did not complete")
        # 사고 과정 thought 조각을 제외하고 사용자에게 보여 줄 text만 연결한다.
        parts = candidate.get("content", {}).get("parts", [])
        text = "".join(p["text"] for p in parts if not p.get("thought") and isinstance(p.get("text"), str)).strip()
        if not text or len(text) > 5000:
            raise ValueError("empty or oversized response")
        usage = payload.get("usageMetadata", {})
        return GeneratedAnswer(text, TokenUsage(
            input_tokens=usage.get("promptTokenCount"), output_tokens=usage.get("candidatesTokenCount"),
            total_tokens=usage.get("totalTokenCount"),
        ))
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as error:
        # 외부 원문·키를 오류 응답이나 로그에 담지 않는다.
        raise ServiceError(502, "AI_RESPONSE_ERROR", "AI 답변 형식·차단 결과·출력 길이를 확인한다") from error


class GeminiGateway:
    # 앞선 google-genai SDK 대신 같은 Gemini API를 HTTPX로 호출해 기존 공유 풀을 이용한다.
    def __init__(self, http: httpx.AsyncClient, settings: Settings):
        self.http, self.settings = http, settings

    async def generate(self, messages: list[dict], style: str) -> GeneratedAnswer:
        # 준비한 질문 → 요청 JSON → HTTP 응답 → 답변 글·사용량 순서로 읽는다.
        if not self.settings.gemini_api_key:
            raise ServiceError(503, "AI_NOT_CONFIGURED", "FastAPI의 GEMINI_API_KEY를 설정한다")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.settings.gemini_model}:generateContent"
        try:
            # HTTPX의 비활성 시간 제한과 별개로 전체 수신 시간도 제한한다.
            # 조금씩 도착하는 JSON이 생성 잠금 수명을 넘어 계속 기다리게 하지 않는다.
            with fail_after(self.settings.ai_timeout_seconds):
                response = await self.http.post(
                    url, headers={"x-goog-api-key": self.settings.gemini_api_key},
                    json=generation_body(messages, style, self.settings.ai_max_output_tokens),
                    timeout=self.settings.ai_timeout_seconds,
                )
        except (httpx.TimeoutException, TimeoutError) as error:
            raise ServiceError(504, "AI_TIMEOUT", "AI 응답 시간이 초과되었다. 저장된 질문은 유지된다") from error
        except httpx.RequestError as error:
            raise ServiceError(502, "AI_UNAVAILABLE", "AI 서비스에 연결할 수 없다. 저장된 질문은 유지된다") from error
        if response.status_code == 429:
            raise ServiceError(429, "AI_RATE_LIMIT", "AI 사용 한도를 확인하고 잠시 뒤 요청한다")
        if response.is_error:
            raise ServiceError(502, "AI_REJECTED", "AI 키·모델·서비스 상태를 확인한다")
        try:
            payload = response.json()
        except ValueError as error:
            raise ServiceError(502, "AI_RESPONSE_ERROR", "AI JSON 응답을 확인할 수 없다") from error
        return parse_answer(payload)

    async def stream(self, messages: list[dict], style: str) -> AsyncIterator[GenerationEvent]:
        # DB 문맥과 답변 지시를 Gemini의 SSE 요청으로 보낸다. 호출자는 delta와 완성 answer를 받는다.
        if not self.settings.gemini_api_key:
            raise ServiceError(503, 'AI_NOT_CONFIGURED', 'FastAPI의 GEMINI_API_KEY를 설정한다')
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{self.settings.gemini_model}:streamGenerateContent'
        text, reason, usage = '', None, {}
        try:
            deadline = monotonic() + self.settings.ai_timeout_seconds
            # URL·헤더·JSON·시간 제한으로 요청을 조립한다. build_request만으로 HTTP를 보내지는 않는다.
            request = self.http.build_request('POST', url, params={'alt': 'sse'},
                headers={'x-goog-api-key': self.settings.gemini_api_key},
                json=generation_body(messages, style, self.settings.ai_max_output_tokens),
                timeout=self.settings.ai_timeout_seconds)
            # stream=True는 응답 본문 전체를 기다리지 않는다. 이 응답만 아래 finally에서 닫는다.
            with fail_after(self.settings.ai_timeout_seconds):
                # 모델 SSE 응답을 본문 전체 대기 없이 받는다.
                response = await self.http.send(request, stream=True)
            try:
                if response.status_code == 429:
                    raise ServiceError(429, 'AI_RATE_LIMIT', 'AI 사용 한도를 확인한다')
                if response.is_error:
                    raise ServiceError(502, 'AI_REJECTED', 'AI 키·모델·서비스 상태를 확인한다')
                if 'text/event-stream' not in response.headers.get('content-type', ''):
                    raise ValueError('SSE content type required')
                # 제공된 파서는 SSE의 빈 줄 경계를 모아 JSON 한 건씩 반환한다.
                # 외부 모델 사건을 GenerationEvent로 바꿔 서비스의 INSERT·done 처리와 분리한다.
                records = json_events(response.aiter_lines())
                while True:
                    # anext는 비동기 제너레이터의 다음 값을 기다린다. 남은 전체 예산만 허용한다.
                    # yield는 시간 제한 블록 밖에서 실행해 취소 범위를 수신자에게 넘기지 않는다.
                    with fail_after(max(0, deadline - monotonic())):
                        try:
                            payload = await anext(records)
                        except StopAsyncIteration:
                            break
                    usage = payload.get('usageMetadata', usage)
                    candidates = payload.get('candidates', [])
                    if not candidates:
                        if payload.get('promptFeedback', {}).get('blockReason'):
                            raise ServiceError(502, 'AI_RESPONSE_ERROR', 'AI 요청이 차단되었다')
                        continue
                    candidate = candidates[0]
                    if candidate.get('finishReason'):
                        reason = candidate['finishReason']
                    delta = ''.join(p['text'] for p in candidate.get('content', {}).get('parts', [])
                                    if not p.get('thought') and isinstance(p.get('text'), str))
                    text += delta
                    if len(text) > 5000:
                        raise ValueError('oversized response')
                    if delta:
                        yield GenerationEvent(delta=delta)
            finally:
                # 요청 취소 중에도 최대 5초 동안 응답 연결을 정리한다. 공유 HTTP 풀은 유지한다.
                with move_on_after(5, shield=True):
                    await response.aclose()
            # 조각 표시와 완료를 구분한다. 정상 종료·길이 검증 뒤에만 완성 답변을 넘긴다.
            if reason != 'STOP':
                if reason == 'MAX_TOKENS':
                    raise ServiceError(502, 'AI_OUTPUT_LIMIT', '출력 한도로 답변이 잘렸다. 부분 글은 저장하지 않는다')
                raise ServiceError(502, 'AI_STREAM_INCOMPLETE', 'AI 응답이 정상 완료되지 않았다. 부분 글은 저장하지 않는다')
            answer = parse_answer({'candidates': [{'finishReason': reason, 'content': {'parts': [{'text': text}]}}],
                                   'usageMetadata': usage})
            yield GenerationEvent(answer=answer)
        except (httpx.TimeoutException, TimeoutError) as error:
            raise ServiceError(504, 'AI_TIMEOUT', 'AI 응답 시간이 초과되었다. 질문 원본은 유지된다') from error
        except httpx.RequestError as error:
            raise ServiceError(502, 'AI_UNAVAILABLE', 'AI 수신 연결이 끊겼다. 부분 글은 저장하지 않는다') from error
        except (ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
            raise ServiceError(502, 'AI_RESPONSE_ERROR', 'AI 스트림 형식을 확인할 수 없다') from error
