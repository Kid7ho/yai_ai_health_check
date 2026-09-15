"""자막 없는 영상의 Gemini 영상 이해 전사. 네트워크를 타지 않는다."""

from __future__ import annotations

import json

import httpx
import pytest

from healthcheck.adapters.youtube.fallback import CaptionsThenAsrExtractor
from healthcheck.adapters.youtube.gemini import GEMINI_CONFIDENCE, GeminiVideoError, GeminiVideoTranscriber
from healthcheck.adapters.youtube.transcript import TranscriptFetchError
from healthcheck.domain.types import ExtractedContent

TEXT = "홍삼을 꾸준히 드시면 면역력이 강화됩니다. 피로 회복에도 좋습니다."


def ok_body(text: str = TEXT, *, thought: str | None = None) -> dict:
    parts = ([{"text": thought, "thought": True}] if thought else []) + [{"text": text}]
    return {
        "candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 5515, "candidatesTokenCount": 120},
    }


def transcriber(handler) -> GeminiVideoTranscriber:
    return GeminiVideoTranscriber("test-key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_유튜브_주소를_넘기고_키는_헤더로_보낸다():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=ok_body())

    content = await transcriber(handler).transcribe("0uEIFaWpd50")

    request = seen[0]
    body = json.loads(request.content)
    assert str(request.url).endswith("/models/gemini-3.5-flash-lite:generateContent")
    assert request.headers["x-goog-api-key"] == "test-key"
    assert "test-key" not in str(request.url)
    assert body["contents"][0]["parts"][0]["file_data"]["file_uri"] == "https://www.youtube.com/watch?v=0uEIFaWpd50"
    assert body["generationConfig"]["temperature"] == 0
    assert content.has_transcript and content.text == TEXT
    assert content.confidence == GEMINI_CONFIDENCE
    assert content.source == "gemini"


async def test_생각_과정_조각은_본문에_넣지_않는다():
    content = await transcriber(lambda r: httpx.Response(200, json=ok_body(thought="먼저 영상을 보겠다"))).transcribe("v")
    assert content.text == TEXT


@pytest.mark.parametrize("text", ["없음", "없음.", "", "짧음"])
async def test_옮길_내용이_없으면_본문_없음으로_본다(text):
    content = await transcriber(lambda r: httpx.Response(200, json=ok_body(text))).transcribe("v")
    assert content.has_transcript is False


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_한도_초과와_서버_오류는_다시_시도할_실패다(status):
    handler = lambda r: httpx.Response(status, json={"error": {"message": "quota"}})
    with pytest.raises(GeminiVideoError):
        await transcriber(handler).transcribe("v")
    assert issubclass(GeminiVideoError, TranscriptFetchError)


async def test_읽을_수_없는_영상은_본문_없음으로_본다():
    handler = lambda r: httpx.Response(400, json={"error": {"message": "private video"}})
    content = await transcriber(handler).transcribe("v")
    assert content.has_transcript is False


class Captions:
    def __init__(self, content: ExtractedContent) -> None:
        self.content = content
        self.calls = 0

    async def fetch(self, video_id):
        self.calls += 1
        return self.content


class Recorder:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.calls = 0
        self.result = result
        self.error = error

    async def transcribe(self, video_id):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


async def test_자막이_있으면_Gemini를_부르지_않는다():
    gemini = Recorder()
    captions = Captions(ExtractedContent(text="자막", has_transcript=True, confidence=0.65))
    content = await CaptionsThenAsrExtractor(captions, gemini).fetch("v")
    assert content.text == "자막" and gemini.calls == 0


async def test_자막이_없으면_Gemini로_옮긴다():
    gemini = Recorder(result=ExtractedContent(text=TEXT, has_transcript=True, confidence=0.75, source="gemini"))
    captions = Captions(ExtractedContent(text="", has_transcript=False, confidence=0.0))
    content = await CaptionsThenAsrExtractor(captions, gemini).fetch("v")
    assert content.source == "gemini" and gemini.calls == 1


async def test_Gemini의_일시적_실패는_올려서_캐시하지_않게_한다():
    gemini = Recorder(error=GeminiVideoError("429"))
    captions = Captions(ExtractedContent(text="", has_transcript=False, confidence=0.0))
    with pytest.raises(TranscriptFetchError):
        await CaptionsThenAsrExtractor(captions, gemini).fetch("v")
