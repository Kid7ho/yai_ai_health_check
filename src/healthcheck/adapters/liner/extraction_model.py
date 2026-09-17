"""Liner Model API로 주장 구절을 옮겨 적는다.

확인한 스펙 (liner.com/llms-full.txt, 2026-09-17 실측 23회)
  POST https://platform.liner.com/api/v1/chat/completions
  헤더 Authorization: Bearer <키>
  요청 model="liner-mark-1.0", messages(system/user), response_format={"type":"json_schema",
       "json_schema":{"name","strict","schema"}}, reasoning_effort(none|low|medium|high|max)
  응답 choices[0].message.content(JSON 문자열), usage.prompt_tokens/completion_tokens
  오류 {"error":{"code","message","retryable"}}. retryable=true면 Retry-After를 따르고 없으면 지수 백오프
  웹 검색을 하지 않는다. temperature·seed는 받기만 하고 적용하지 않는다 → 결과 재현성을 설정으로
  고정할 수 없으므로 원문 대조 검사(ClaimValidator)가 반드시 필요하다.

Agent API(Quick Answer 등)는 쓰지 않는다. 웹 검색을 끌 수 없고 시스템 지시문·출력 형식을 받지 않으며,
실측에서 지시를 무시하고 검색·평가를 덧붙였다.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import httpx

from healthcheck.ports.extraction_model import ExtractionModelError

CHAT_URL = "https://platform.liner.com/api/v1/chat/completions"
MODEL = "liner-mark-1.0"
EFFORTS = ("none", "low", "medium", "high", "max")


class LinerExtractionModel:
    def __init__(
        self,
        api_key: str,
        *,
        reasoning_effort: str = "low",
        timeout: float = 90.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("LINER_API_KEY가 없습니다.")
        if reasoning_effort not in EFFORTS:
            raise ValueError(f"reasoning_effort는 {EFFORTS} 중 하나입니다.")
        self._api_key = api_key
        self._effort = reasoning_effort
        self._timeout = timeout
        self._max_retries = max_retries
        self._client = client
        self._owns_client = client is None
        # 비용 확인용 누적 토큰. 요청 본문은 남기지 않는다.
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    @property
    def name(self) -> str:
        return f"{MODEL}/{self._effort}"

    async def extract_json(self, *, instructions: str, document: str, schema: dict[str, Any], quick: bool = False) -> dict[str, Any]:
        body = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": document},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "claim_extraction", "strict": True, "schema": schema},
            },
            # 식품 이름 목록(quick)은 추론을 low로 줄인다. 운영 실측에서 두 단계 추출이 44초 걸렸다.
            "reasoning_effort": "low" if quick and self._effort not in ("none", "low") else self._effort,
        }
        client = self._ensure_client()
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

        for attempt in range(self._max_retries + 1):
            try:
                response = await client.post(CHAT_URL, json=body, headers=headers, timeout=self._timeout)
            except httpx.HTTPError as error:
                if attempt < self._max_retries:
                    await asyncio.sleep(_backoff(attempt))
                    continue
                raise ExtractionModelError(f"Liner 통신 오류: {type(error).__name__}") from None

            if response.status_code == 200:
                return self._parse(response)

            error = _error_body(response)
            retryable = error.get("retryable")
            if retryable is None:
                retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < self._max_retries:
                await asyncio.sleep(_retry_after(response, _backoff(attempt)))
                continue
            raise ExtractionModelError(f"Liner 응답 {response.status_code} {error.get('code', '')}")
        raise ExtractionModelError("Liner 재시도 초과")  # pragma: no cover

    def _parse(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError):
            raise ExtractionModelError("Liner 응답 형식이 예상과 다릅니다") from None
        self.usage["calls"] += 1
        self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        try:
            parsed = json.loads(content)
        except ValueError:
            raise ExtractionModelError("Liner 출력이 JSON이 아닙니다") from None
        if not isinstance(parsed, dict):
            raise ExtractionModelError("Liner 출력이 JSON 객체가 아닙니다")
        return parsed

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


def _error_body(response: httpx.Response) -> dict[str, Any]:
    try:
        error = response.json().get("error")
    except ValueError:
        return {}
    return error if isinstance(error, dict) else {}


def _backoff(attempt: int) -> float:
    return min(8.0, 1.0 * (2**attempt)) + random.uniform(0, 0.5)


def _retry_after(response: httpx.Response, default: float) -> float:
    try:
        return max(0.0, min(float(response.headers.get("retry-after", default)), 10.0))
    except ValueError:
        return default
