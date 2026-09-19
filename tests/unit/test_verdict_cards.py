"""발표 자료 목업에 맞춘 결과·근거 카드.

카카오 응답은 글자색·배경색을 지정할 수 없어 신호등 색 머리 부분을 라벨별 이미지로
대신한다. 판정 이유(규칙표 안내)는 결과 카드에서 빼고 근거 화면으로 옮겼다.
결과 카드 뒤에는 "다른 영상도 보내주시면" 말풍선을 붙인다.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from healthcheck.api.app import VERDICT_IMAGE_DIR, build_fake_orchestrator, create_app
from healthcheck.adapters.fake.callback import RecordingCallbackSender
from healthcheck.domain.labels import Label
from healthcheck.pipeline import step1_input
from healthcheck.pipeline.step6_response import (
    DISCLAIMER,
    IMAGE_FILES,
    IMAGE_VERSION,
    render,
    render_reason,
    verdict_image_url,
)

BASE = "https://example.ngrok-free.dev"
TEXT = "홍삼이 면역력 증진에 도움을 줄 수 있다"


async def result_of(text: str = TEXT):
    return await build_fake_orchestrator().run(step1_input.classify(text))


async def test_이미지_주소가_있으면_라벨_이미지_카드로_답한다():
    result = await result_of()

    outputs = render(result, image_base_url=BASE)["template"]["outputs"]
    card = outputs[0]["basicCard"]

    assert card["thumbnail"]["imageUrl"] == verdict_image_url(BASE, result.verdict.label)
    assert card["thumbnail"]["imageUrl"].startswith(f"{BASE}/static/verdict/{IMAGE_VERSION}/")
    # 판정 대상 문장이 진한 글씨(제목)로 나간다.
    assert "홍삼" in card["title"]
    assert card["description"].startswith("이 내용을 확인했어요")
    assert len(card["description"]) <= 230
    assert [b["label"] for b in card["buttons"]] == ["왜 그런지 보기", "친구에게 보내기"]


async def test_결과_카드에는_판정_이유를_넣지_않는다():
    result = await result_of()
    assert result.verdict.guidance

    for payload in (render(result), render(result, image_base_url=BASE)):
        card = payload["template"]["outputs"][0]
        body = card.get("basicCard", card.get("textCard"))["description"]
        assert result.verdict.guidance not in body


async def test_결과는_카드_하나로_보내고_면책_문구는_카드_안에_붙는다():
    """어르신 화면에 말풍선이 쌓여 가독성이 떨어졌다. 확인 한 번에 카드 하나만 보낸다."""
    result = await result_of()

    for payload in (render(result), render(result, image_base_url=BASE)):
        outputs = payload["template"]["outputs"]
        assert len(outputs) == 1
        card = outputs[0].get("basicCard") or outputs[0]["textCard"]
        assert card["description"].endswith(DISCLAIMER)
        assert "다른 영상도" not in str(payload)


async def test_근거_화면에서_판정_이유를_보여_준다():
    result = await result_of()

    outputs = render_reason(result, image_base_url=BASE)["template"]["outputs"]
    card = outputs[0]["basicCard"]

    assert card["title"] == "왜 그런가요"
    assert card["description"].startswith(result.verdict.guidance)
    assert card["description"].endswith(DISCLAIMER)
    assert len(card["description"]) <= 230
    assert card["thumbnail"]["imageUrl"] == verdict_image_url(BASE, result.verdict.label)
    assert len(outputs) == 1


async def test_이미지_주소가_없으면_근거를_한_말풍선에_담는다():
    result = await result_of()

    outputs = render_reason(result)["template"]["outputs"]

    assert len(outputs) == 1
    assert result.verdict.guidance in outputs[0]["simpleText"]["text"]
    assert outputs[0]["simpleText"]["text"].endswith(DISCLAIMER)


async def test_범위_밖_안내에는_이미지와_다음_안내를_붙이지_않는다():
    result = await result_of("당뇨약 끊고 이 즙만 드세요")

    outputs = render(result, image_base_url=BASE)["template"]["outputs"]

    assert all("basicCard" not in o for o in outputs)
    assert len(outputs) == 1


def test_라벨마다_이미지_파일이_있다():
    assert set(IMAGE_FILES) == set(Label)
    for name in IMAGE_FILES.values():
        data = (VERDICT_IMAGE_DIR / f"{name}.png").read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_이미지는_인증_없이_내려받고_다른_파일은_막는다():
    app = create_app(
        orchestrator=build_fake_orchestrator(),
        callback_sender=RecordingCallbackSender(),
        image_base_url=BASE,
    )
    with TestClient(app) as client:
        ok = client.get(f"/static/verdict/{IMAGE_VERSION}/warning.png")
        assert ok.status_code == 200
        assert ok.headers["content-type"] == "image/png"

        assert client.get("/static/verdict/v0/warning.png").status_code == 404
        assert client.get(f"/static/verdict/{IMAGE_VERSION}/debug.png").status_code == 404
        assert client.get(f"/static/verdict/{IMAGE_VERSION}/..%2F..%2Fapp.png").status_code == 404


def test_콜백으로_이미지_카드를_보낸다():
    sender = RecordingCallbackSender()
    app = create_app(
        orchestrator=build_fake_orchestrator(), callback_sender=sender, image_base_url=BASE
    )
    body = {"userRequest": {"utterance": TEXT, "user": {"id": "u1"}, "callbackUrl": "https://bot-api.kakao.com/cb"}}
    with TestClient(app) as client:
        client.post("/skill", json=body)
        for _ in range(50):
            if sender.sent:
                break
            import time

            time.sleep(0.05)
        reason = client.post("/skill", json={**body, "userRequest": {**body["userRequest"], "utterance": "왜 그런지 보기"}}).json()

    card = sender.last.payload["template"]["outputs"][0]["basicCard"]
    assert card["thumbnail"]["imageUrl"].startswith(BASE)
    assert reason["template"]["outputs"][0]["basicCard"]["title"] == "왜 그런가요"


def test_공개_주소는_https만_받는다(monkeypatch):
    import pytest

    from healthcheck.config import load_settings

    monkeypatch.setenv("PUBLIC_BASE_URL", "http://example.com")
    with pytest.raises(ValueError):
        load_settings(env_file=None)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.com/")
    assert load_settings(env_file=None).public_base_url == "https://example.com"
