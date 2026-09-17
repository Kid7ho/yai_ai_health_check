"""녹화·재생 전송 단위 테스트. 네트워크를 타지 않는다."""

from __future__ import annotations

import json

import httpx
import pytest

from healthcheck.testing.replay import RecordReplayTransport, ReplayMissingError, request_key, replay_orchestrator


class FakeInner(httpx.AsyncBaseTransport):
    def __init__(self, status: int) -> None:
        self.status = status

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self.status, json={"ok": self.status == 200}, headers={"retry-after": "1"})


async def test_호출_제한과_서버_오류는_녹화하지_않는다(tmp_path):
    """녹화하면 일시적인 429가 재생마다 굳어 입장 분류가 늘 표현 목록으로 떨어진다."""
    path = tmp_path / "rec.json"
    transport = RecordReplayTransport(path, "record")
    transport._inner = FakeInner(429)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post("https://example.org/x", json={"a": 1})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    transport.save()
    assert json.loads(path.read_text(encoding="utf-8")) == {}


async def test_재생_중_녹화에_없는_요청을_모은다(tmp_path):
    path = tmp_path / "rec.json"
    path.write_text("{}", encoding="utf-8")
    transport = RecordReplayTransport(path, "replay")
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ReplayMissingError):
            await client.post("https://example.org/x", json={"a": 1})
    request = httpx.Request("POST", "https://example.org/x", json={"a": 1})
    assert transport.missing == {request_key(request)}


def test_분류_시트용_조립은_언어_모델_입장_분류를_쓴다(tmp_path):
    path = tmp_path / "rec.json"
    path.write_text("{}", encoding="utf-8")
    orchestrator, _ = replay_orchestrator(path, "replay", stance_model=True)
    assert orchestrator.academic._stance_classifier.name.startswith("llm:liner-mark-1.0/none+verify:")
    plain, _ = replay_orchestrator(path, "replay")
    assert plain.academic._stance_classifier is None
