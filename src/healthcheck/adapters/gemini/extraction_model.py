"""Gemini로 주장 구절을 옮겨 적는다(비교용·대안).

요청 형식은 이미 쓰는 영상 받아 적기 어댑터(adapters/youtube/gemini.py)와 같은 generateContent다.
  systemInstruction.parts[].text  지시문
  generationConfig.temperature=0  같은 입력이면 같은 출력을 기대한다(Liner는 이 값을 적용하지 않는다)
  generationConfig.responseMimeType="application/json", responseSchema  출력 형식 강제

주의: 무료 등급은 보낸 내용이 제품 개선에 쓰인다. 운영에 쓰려면 유료 등급으로 바꾼다.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import httpx

from healthcheck.ports.extraction_model import ExtractionModelError

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """JSON Schema를 Gemini responseSchema 형식으로 옮긴다. additionalProperties는 지원하지 않아 뺀다."""
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        if key == "type":
            out["type"] = str(value).upper()
        elif key == "properties":
            out["properties"] = {name: to_gemini_schema(sub) for name, sub in value.items()}
        elif key == "items":
            out["items"] = to_gemini_schema(value)
        else:
            out[key] = value
    return out


class GeminiExtractionModel:
    def __init__(self, api_key: str, *, model: str = "gemini-3.5-flash-lite", timeout: float = 90.0, max_retries: int = 2, client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY가 없습니다.")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self._client = client
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    @property
    def name(self) -> str:
        return self._model

    async def extract_json(self, *, instructions: str, document: str, schema: dict[str, Any], quick: bool = False) -> dict[str, Any]:
        body = {
            "systemInstruction": {"parts": [{"text": instructions}]},
            "contents": [{"role": "user", "parts": [{"text": document}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": to_gemini_schema(schema),
            },
        }
        if self._client is None:
            self._client = httpx.AsyncClient()
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(
                    GEMINI_URL.format(model=self._model), headers={"x-goog-api-key": self._api_key}, json=body, timeout=self._timeout
                )
            except httpx.HTTPError as error:
                if attempt < self._max_retries:
                    await asyncio.sleep(1 + attempt * 2 + random.random())
                    continue
                raise ExtractionModelError(f"Gemini 통신 오류: {type(error).__name__}") from None
            if response.status_code == 200:
                return self._parse(response)
            if (response.status_code == 429 or response.status_code >= 500) and attempt < self._max_retries:
                await asyncio.sleep(5 * (attempt + 1) + random.random())
                continue
            raise ExtractionModelError(f"Gemini 응답 {response.status_code}: {response.text[:200]}")
        raise ExtractionModelError("Gemini 재시도 초과")  # pragma: no cover

    def _parse(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
            text = "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
            usage = data.get("usageMetadata") or {}
        except (ValueError, KeyError, IndexError, TypeError):
            raise ExtractionModelError("Gemini 응답 형식이 예상과 다릅니다") from None
        self.usage["calls"] += 1
        self.usage["prompt_tokens"] += int(usage.get("promptTokenCount") or 0)
        self.usage["completion_tokens"] += int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0)
        try:
            parsed = json.loads(text)
        except ValueError:
            raise ExtractionModelError("Gemini 출력이 JSON이 아닙니다") from None
        if not isinstance(parsed, dict):
            raise ExtractionModelError("Gemini 출력이 JSON 객체가 아닙니다")
        return parsed
