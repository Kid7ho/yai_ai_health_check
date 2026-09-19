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
    assert carousel["items"][-1]["buttons"][0]["messageText"].startswith("한번 해 보기\n")
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


# ─────────────────────────────────────────────────────────────
# 주소가 아닌 말
# ─────────────────────────────────────────────────────────────

from healthcheck.pipeline.small_talk import classify_small_talk  # noqa: E402


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("안녕하세요", "greeting"),
        ("안녕하세요~", "greeting"),
        ("반갑습니다", "greeting"),
        ("감사합니다", "thanks"),
        ("고마워요 ㅎㅎ", "thanks"),
        ("네 알겠습니다", "thanks"),
        ("홍삼 먹어도 돼요?", "question"),
        ("당뇨에 뭐가 좋아요", "question"),
        ("오늘 날씨가 좋네요 산책 다녀왔어요", "off_topic"),
        ("점심 뭐 먹을까", "off_topic"),
        # 확인 과정으로 보내는 말
        ("홍삼이 면역력 증진에 도움을 줄 수 있다", None),
        ("홍삼이 면역력에 좋다는데 사실인가요?", None),
        ("혈압약 끊고 양파즙 먹으면 되나요", None),
        ("블루베리로 당뇨 완치", None),
        ("", None),
    ],
)
def test_주소가_아닌_말을_가른다(text, expected):
    assert classify_small_talk(text) == expected


@pytest.mark.parametrize(
    ("utterance", "starts"),
    [
        ("안녕하세요", "안녕하세요! 받은 건강 정보를"),
        ("감사합니다", "도움이 되셨다니 다행이에요."),
        ("홍삼 먹어도 돼요?", "궁금한 점에 직접 답해 드리진 못해요."),
        ("오늘 날씨가 좋네요", "건강 정보가 담긴"),
    ],
)
def test_주소가_아닌_말은_기다리지_않고_바로_답한다(utterance, starts):
    payload, sender = post(kakao_request(utterance))
    assert payload.get("useCallback") is not True
    assert payload["template"]["outputs"][0]["simpleText"]["text"].startswith(starts)
    assert quick_labels(payload) == ["사용법 보기", "한번 해 보기"]
    assert sender.sent == []


def test_건강_주장_문장은_지금처럼_확인_과정으로_간다():
    payload, _ = post(kakao_request("홍삼이 면역력 증진에 도움을 줄 수 있다"))
    assert payload.get("useCallback") is True


# ─────────────────────────────────────────────────────────────
# 채팅방 아래 고정 메뉴(사용법 · 한 번 해보기 · 주의사항)
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label", ["사용법", "한 번 해보기", "주의사항"])
def test_고정_메뉴_세_버튼이_보내는_말에_바로_답한다(label):
    payload, _ = post(kakao_request(label))
    if label == "한 번 해보기":
        # 띄어쓰기가 달라도 [한번 해 보기]와 같은 말로 본다
        assert payload.get("useCallback") is True
        return
    assert payload.get("useCallback") is not True
    output = payload["template"]["outputs"][0]
    if label == "사용법":
        assert "carousel" in output
    else:
        assert output["textCard"]["title"] == "꼭 알아 두세요"
        assert "진단이 아닙니다" in output["textCard"]["description"]
        assert len(output["textCard"]["description"]) <= 400


def test_한번_해_보기는_기다리는_동안_확인하는_문장을_보여_준다():
    payload, _ = post(kakao_request("한 번 해보기"))
    assert payload["useCallback"] is True
    assert "홍삼이 면역력 증진에 도움을 줄 수 있다" in payload["data"]["text"]


def test_한번_해_보기_결과_위에_보낸_문장과_보내는_법을_붙인다():
    from healthcheck.pipeline.step6_response import try_intro

    payload = render(result(Label.TRUSTWORTHY), intro=try_intro())
    first = payload["template"]["outputs"][0]["simpleText"]["text"]
    assert first.startswith("이렇게 보내 보세요")
    assert "「홍삼이 면역력 증진에 도움을 줄 수 있다」" in first


def test_한번_해_보기_버튼은_예시_문장까지_두_줄로_보낸다():
    from healthcheck.pipeline.step6_response import render_welcome, try_message

    assert try_message() == "한번 해 보기\n홍삼이 면역력 증진에 도움을 줄 수 있다"
    replies = render_welcome()["template"]["quickReplies"]
    assert replies[1]["label"] == "한번 해 보기"
    assert replies[1]["messageText"] == try_message()
    card_button = render_guide()["template"]["outputs"][0]["carousel"]["items"][-1]["buttons"][0]
    assert card_button["messageText"] == try_message()


@pytest.mark.parametrize(
    "utterance",
    ["한번 해 보기\n홍삼이 면역력 증진에 도움을 줄 수 있다", "한 번 해보기\n홍삼이 면역력 증진에 도움을 줄 수 있다", "한 번 해보기"],
)
def test_두_줄로_온_체험_버튼도_체험으로_본다(utterance):
    assert guide_command(utterance) == "try"
