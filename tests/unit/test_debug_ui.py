"""디버그 채팅 화면 테스트.

개발용 도구이므로 검증 범위는 셋이다.
  판정 로직을 우회하지 않는가 (면책 문구 포함)
  중간 산출물을 빠뜨리지 않는가
  배포에서 끌 수 있는가
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from healthcheck.api.app import build_fake_orchestrator, create_app
from healthcheck.pipeline.step6_response import DISCLAIMER


def client_for(monkeypatch=None, debug: bool = True) -> TestClient:
    if monkeypatch is not None:
        monkeypatch.setenv("DEBUG_UI", "1" if debug else "0")
    return TestClient(create_app(build_fake_orchestrator(), worker_count=0))


def test_디버그_화면이_열린다(monkeypatch):
    with client_for(monkeypatch) as client:
        response = client.get("/debug")
    assert response.status_code == 200
    assert "디버그 패널" in response.text


def test_DEBUG_UI가_0이면_화면이_없다(monkeypatch):
    """배포 환경에서는 끈다."""
    with client_for(monkeypatch, debug=False) as client:
        assert client.get("/debug").status_code == 404
        assert client.post("/debug/check", json={"utterance": "홍삼"}).status_code == 404


def test_발화를_넣으면_판정과_중간_산출물을_돌려준다(monkeypatch):
    with client_for(monkeypatch) as client:
        body = client.post(
            "/debug/check",
            json={"utterance": "냉동 블루베리가 당뇨를 완치시킨다", "mode": "fake"},
        ).json()

    assert body["verdict"]["label"] == "WARNING"
    # Fake 식약처 데이터에 블루베리·당뇨 부당광고 이력이 있어 R-020이 먼저 잡는다.
    assert body["verdict"]["rule_id"] == "R-020"
    assert body["verdict"]["rules_version"] == "v0.11"
    assert body["input"]["kind"] == "PLAIN_TEXT"
    assert body["claims"]["disease_cure_expression"] is True
    assert body["verdict"]["inputs"]["transcript_quality"] == "ok"
    assert body["timings_ms"]["total"] >= 0


def test_카카오_페이로드를_그대로_보여_준다(monkeypatch):
    """이식할 때 이 페이로드를 기준으로 쓴다."""
    with client_for(monkeypatch) as client:
        body = client.post(
            "/debug/check", json={"utterance": "홍삼이 면역력에 도움이 된다", "mode": "fake"}
        ).json()

    assert body["ack_payload"]["useCallback"] is True
    texts = [
        output.get("simpleText", {}).get("text") or output.get("textCard", {}).get("description", "")
        for output in body["callback_payload"]["template"]["outputs"]
    ]
    assert any(DISCLAIMER in t for t in texts)


def test_유튜브_링크도_받는다(monkeypatch):
    with client_for(monkeypatch) as client:
        body = client.post(
            "/debug/check", json={"utterance": "https://youtu.be/redginseng1", "mode": "fake"}
        ).json()

    assert body["input"]["kind"] == "YOUTUBE"
    assert body["input"]["video_id"] == "redginseng1"
    assert body["content"]["has_transcript"] is True
    assert body["verdict"]["label"] == "TRUSTWORTHY"


def test_fake_모드는_실제_API를_쓰지_않는다(monkeypatch):
    """규칙만 시험할 때 토큰을 아낀다."""
    with client_for(monkeypatch) as client:
        body = client.post(
            "/debug/check", json={"utterance": "홍삼이 면역력에 좋다", "mode": "fake"}
        ).json()

    assert body["adapters"]["academic"] == "FakeAcademicSearchClient"


def test_빈_발화는_오류로_알려_준다(monkeypatch):
    with client_for(monkeypatch) as client:
        body = client.post("/debug/check", json={"utterance": "  "}).json()
    assert "발화가 비었습니다" in body["error"]
