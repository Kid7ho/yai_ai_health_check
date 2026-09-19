"""사용법 안내 테스트. 사용법 카드, 처음 인사, [한번 해 보기], 결과 뒤 사용법 버튼."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from healthcheck.adapters.fake.callback import RecordingCallbackSender
from healthcheck.api.app import build_fake_orchestrator, create_app
from healthcheck.domain.labels import Label
from healthcheck.pipeline.step6_response import DISCLAIMER, guide_command, load_guide, render, render_guide

from .test_response_disclaimer import result
from .test_skill_server import kakao_request


def post(body: dict) -> tuple[dict, RecordingCallbackSender]:
    sender = RecordingCallbackSender()
    app = create_app(orchestrator=build_fake_orchestrator(), callback_sender=sender)
    with TestClient(app) as client:
        payload = client.post("/skill", json=body).json()
    return payload, sender


def quick_labels(payload: dict) -> list[str]:
    return [q["label"] for q in payload.get("template", {}).get("quickReplies", [])]


@pytest.mark.parametrize("utterance", ["사용법", "사용법 보기", "사용법?", "어떻게 써요?", "도움말"])
def test_사용법을_물으면_넘겨_보는_카드_네_장으로_바로_답한다(utterance):
    payload, sender = post(kakao_request(utterance))
    assert payload.get("useCallback") is not True
    carousel = payload["template"]["outputs"][0]["carousel"]
    assert carousel["type"] == "textCard"
    assert [item["title"] for item in carousel["items"]] == [
        "이렇게 보내세요",
        "공유가 안 되면",
        "15초 뒤 답이 와요",
        "이건 못 봐요",
    ]
    assert carousel["items"][-1]["buttons"][0]["messageText"] == "한번 해 보기"
    assert sender.sent == []


def test_사용법_카드는_카카오_글자_수_제한을_지킨다():
    guide = load_guide()
    for card in render_guide()["template"]["outputs"][0]["carousel"]["items"]:
        assert len(card["title"]) <= 50
        assert len(card["description"]) <= 400
    for label in guide["버튼"].values():
        assert len(label) <= 14


def test_웰컴_블록과_처음이라는_말에는_처음_인사로_답한다():
    body = kakao_request("")
    body["intent"]["name"] = "웰컴 블록"
    for request in (body, kakao_request("처음")):
        payload, _ = post(request)
        text = payload["template"]["outputs"][0]["simpleText"]["text"]
        assert text.startswith("안녕하세요, AI 건강지킴이예요.")
        assert quick_labels(payload) == ["사용법 보기", "한번 해 보기"]


def test_한번_해_보기는_예시_문장을_대신_확인한다():
    payload, _ = post(kakao_request("한번 해 보기"))
    # 처음에는 캐시가 없어 기본 경로(즉시 응답 후 콜백)로 간다
    assert payload.get("useCallback") is True


def test_처음_인사는_판정이_아니라_면책_문구를_붙이지_않는다():
    payload, _ = post(kakao_request("처음"))
    assert DISCLAIMER not in payload["template"]["outputs"][0]["simpleText"]["text"]


@pytest.mark.parametrize("label", list(Label))
def test_알_수_없어요에만_사용법_버튼을_단다(label):
    payload = render(result(label))
    expected = ["사용법 보기"] if label == Label.UNKNOWN else []
    assert quick_labels(payload) == expected


def test_범위_밖_거절과_안내에도_사용법_버튼을_단다():
    assert quick_labels(render(result(Label.UNKNOWN, notice="not_target"))) == ["사용법 보기"]


def test_일반_문장은_사용법_말로_보지_않는다():
    assert guide_command("홍삼이 면역력에 좋다") is None
    assert guide_command("https://youtu.be/abc") is None
    assert guide_command("") is None
