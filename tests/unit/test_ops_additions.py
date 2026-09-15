"""녹화 재생, Gemini 사용량·한도 대기, 콜백 유효 시간 시험."""

from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from healthcheck.adapters.fake.callback import RecordingCallbackSender
from healthcheck.adapters.youtube.gemini import GeminiVideoError, GeminiVideoTranscriber
from healthcheck.api.app import build_fake_orchestrator, create_app
from healthcheck.testing.replay import RecordReplayTransport, ReplayMissingError, request_key


# ── 녹화 재생 ────────────────────────────────────────────────


def test_녹화_키에는_식약처_인증키가_남지_않는다():
    request = httpx.Request("GET", "http://openapi.foodsafetykorea.go.kr/api/SECRET123/I-0040/json/1/100/APLC_RAWMTRL_NM=x")
    key = request_key(request)
    assert "SECRET123" not in key
    assert "/api/KEY/I-0040/" in key


async def test_녹화에_없는_요청은_분명한_오류로_알린다(tmp_path):
    path = tmp_path / "rec.json"
    path.write_text("{}", encoding="utf-8")
    client = httpx.AsyncClient(transport=RecordReplayTransport(path, "replay"))
    with pytest.raises(ReplayMissingError):
        await client.post("https://platform.liner.com/api/v1/tools/search/scholar", json={"query": "x"})


async def test_녹화한_응답을_그대로_돌려준다(tmp_path):
    path = tmp_path / "rec.json"
    request = httpx.Request("POST", "https://platform.liner.com/api/v1/tools/search/scholar", json={"query": "x"})
    path.write_text(json.dumps({request_key(request): {"status": 200, "body": '{"results": []}'}}), encoding="utf-8")
    client = httpx.AsyncClient(transport=RecordReplayTransport(path, "replay"))
    response = await client.post("https://platform.liner.com/api/v1/tools/search/scholar", json={"query": "x"})
    assert response.json() == {"results": []}


# ── Gemini 사용량과 한도 대기 ────────────────────────────────


def ok(tokens_in=100, tokens_out=20):
    return httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": "홍삼을 드시면 면역력에 도움이 됩니다."}]}}],
        "usageMetadata": {"promptTokenCount": tokens_in, "candidatesTokenCount": tokens_out},
    })


async def test_오늘_사용량을_센다():
    g = GeminiVideoTranscriber("k", client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: ok())))
    await g.transcribe("a")
    await g.transcribe("b")
    usage = g.usage()
    assert (usage["calls"], usage["input_tokens"], usage["output_tokens"]) == (2, 200, 40)
    assert usage["limited_now"] is False


async def test_한도_초과를_받으면_잠시_부르지_않는다():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json={"error": {"message": "quota"}})

    g = GeminiVideoTranscriber("k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(GeminiVideoError):
        await g.transcribe("a")
    with pytest.raises(GeminiVideoError):
        await g.transcribe("b")
    assert len(calls) == 1
    assert g.usage()["limited_now"] is True and g.usage()["rate_limited"] == 1


# ── 콜백 유효 시간 시험 ──────────────────────────────────────


def body(text):
    return {"userRequest": {"utterance": text, "user": {"id": "u"}, "callbackUrl": "https://bot-api.kakao.com/cb"}}


def test_시험이_꺼져_있으면_일반_문장으로_처리한다(monkeypatch):
    sender = RecordingCallbackSender()
    app = create_app(orchestrator=build_fake_orchestrator(), callback_sender=sender)
    with TestClient(app) as client:
        client.post("/skill", json=body("콜백시험 0"))
        for _ in range(40):
            if sender.sent:
                break
            time.sleep(0.05)
    assert "콜백 시험" not in json.dumps(sender.last.payload, ensure_ascii=False)


def test_시험을_켜면_지정한_시간_뒤_콜백을_보낸다(monkeypatch):
    monkeypatch.setenv("CALLBACK_PROBE_ENABLED", "1")
    sender = RecordingCallbackSender()
    app = create_app(orchestrator=build_fake_orchestrator(), callback_sender=sender)
    with TestClient(app) as client:
        ack = client.post("/skill", json=body("콜백시험 0")).json()
        for _ in range(40):
            if sender.sent:
                break
            time.sleep(0.05)
    assert ack.get("useCallback") is True
    assert "콜백 시험: 0초 뒤에 보낸 메시지입니다." in json.dumps(sender.last.payload, ensure_ascii=False)
