"""큰 버튼 — 결과 카드의 버튼을 리스트 카드 줄로 보여 주는 모양(켠 사용자만)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from healthcheck.adapters.fake.callback import RecordingCallbackSender
from healthcheck.adapters.inprocess_queue import InProcessJobQueue
from healthcheck.api.app import build_fake_orchestrator
from healthcheck.api.worker import Worker
from healthcheck.pipeline import step1_input
from healthcheck.ports.queue import CheckJob

from healthcheck.api.card_trial import CardLayouts, command_of
from healthcheck.api.share import SHARE_CARDS, with_share_link
from healthcheck.domain.labels import Label
from healthcheck.pipeline.step6_response import DISCLAIMER, render

from .test_events_kpi import USER, app_with_events
from .test_response_disclaimer import result
from .test_skill_server import kakao_request

BASE = "https://example.com"
SOURCE = "https://www.youtube.com/watch?v=RfxAz6RwfpI"
TOKEN = "e" * 32


def test_리스트_모양은_사진_카드_아래에_줄을_붙이고_면책_문구는_사진_카드에_남는다():
    payload = render(result(Label.CAUTION, source_urls=(SOURCE,)), image_base_url=BASE, layout="list")
    photo, listing = payload["template"]["outputs"]
    assert "buttons" not in photo["basicCard"]
    assert photo["basicCard"]["description"].endswith(DISCLAIMER)
    items = listing["listCard"]["items"]
    assert [i["title"] for i in items] == ["🔍 왜 그런지 보기", "🔗 원본 정보 확인하기"]
    assert items[0]["action"] == "message" and items[0]["messageText"] == "왜 그런지 보기"
    assert items[1]["link"] == {"web": SOURCE}
    assert listing["listCard"]["buttons"] == [{"action": "share", "label": "📤 친구에게 보내기"}]


def test_사진_주소가_없으면_리스트_모양도_버튼_카드로_답한다():
    payload = render(result(Label.CAUTION), layout="list")
    assert [list(o) for o in payload["template"]["outputs"]] == [["textCard"]]


def test_공식_공유를_쓰면_공유_버튼도_같은_높이의_줄이_된다():
    SHARE_CARDS.clear()
    payload = render(result(Label.CAUTION, source_urls=(SOURCE,)), image_base_url=BASE, layout="list")
    linked = with_share_link(payload, BASE, TOKEN, web_share=True, source_url=SOURCE, source_secret="admin-secret")
    photo, listing = linked["template"]["outputs"]
    items = listing["listCard"]["items"]
    assert "buttons" not in listing["listCard"]
    assert items[-1]["title"] == "📤 친구에게 보내기" and items[-1]["link"] == {"web": f"{BASE}/share/{TOKEN}"}
    assert items[1]["link"]["web"].startswith(f"{BASE}/o/{TOKEN}?") and "by=me" in items[1]["link"]["web"]
    assert photo["basicCard"]["thumbnail"]["link"] == {"web": f"{BASE}/s/{TOKEN}"}
    assert SHARE_CARDS.get(TOKEN)["source"] == SOURCE


def test_기본은_버튼_카드이고_켠_사용자만_리스트_모양이다():
    assert command_of("큰 버튼 켜기") is True and command_of("큰버튼끄기") is False and command_of("큰 버튼") is None
    layouts = CardLayouts()
    assert layouts.layout_for("a") == "buttons" and layouts.layout_for(None) == "buttons"
    layouts.set("a", True)
    assert layouts.layout_for("a") == "list" and layouts.layout_for("b") == "buttons"
    layouts.set("a", False)
    assert layouts.layout_for("a") == "buttons"


async def test_워커가_보내는_결과는_기본으로_버튼_카드다():
    sender = RecordingCallbackSender()
    worker = Worker(InProcessJobQueue(), build_fake_orchestrator(), sender, image_base_url=BASE)
    await worker.process(
        CheckJob(
            job_id="job-1",
            share_input=step1_input.classify("https://youtu.be/blueberry01"),
            callback_url="https://callback.kakao.example/abc",
            user_key=USER,
        )
    )
    payload = sender.sent[-1].payload
    kinds = [list(o)[0] for o in payload["template"]["outputs"]]
    assert kinds == ["basicCard"]
    assert len(payload["template"]["outputs"][0]["basicCard"]["buttons"]) == 3


def test_켜고_끄면_직전_결과를_바뀐_모양으로_다시_보여_준다():
    app, events = app_with_events()
    with TestClient(app) as client:
        app.state.services.image_base_url = BASE
        first = client.post("/skill", json=kakao_request("큰버튼켜기")).json()
        app.state.last_results.put(USER, result(Label.CAUTION, source_urls=(SOURCE,)))
        on = client.post("/skill", json=kakao_request("큰 버튼 켜기")).json()
        off = client.post("/skill", json=kakao_request("큰 버튼 끄기")).json()
    assert "영상이나 글을 보내 보세요" in first["template"]["outputs"][0]["simpleText"]["text"]
    assert [list(o)[0] for o in on["template"]["outputs"]] == ["simpleText", "basicCard", "listCard"]
    assert [list(o)[0] for o in off["template"]["outputs"]] == ["simpleText", "basicCard"]
    assert len(off["template"]["outputs"][1]["basicCard"]["buttons"]) == 3
    # 시험 명령은 어르신 행동 기록(KPI)에 넣지 않는다
    assert events.events == []
