"""응답 기록과 교육 회차 KPI 테스트.

교육 성과(참여자 수, 성공률, 첫 성공까지 단계, 재사용률, 막힌 뒤 이탈, 공유 클릭)를 서버가
자동으로 세는지 본다. 보낸 글 내용은 기록에 남지 않아야 한다.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from healthcheck.adapters.fake.callback import RecordingCallbackSender
from healthcheck.adapters.inprocess_queue import InProcessJobQueue
from healthcheck.api.app import build_fake_orchestrator, create_app
from healthcheck.api.worker import Worker
from healthcheck.domain.labels import Label
from healthcheck.pipeline import step1_input
from healthcheck.pipeline.step6_response import DISCLAIMER
from healthcheck.ports.events import Event
from healthcheck.ports.queue import CheckJob
from healthcheck.storage.events import InMemoryEventLog, SqliteEventLog, describe_input, new_event, user_hash
from healthcheck.storage.kpi import KST, compute, format_report, parse_cohort
from healthcheck.storage.memory_repo import InMemoryCheckRepository

from .test_response_disclaimer import result
from .test_skill_server import kakao_request

USER = "botuserkey-abc"


def app_with_events():
    events = InMemoryEventLog()
    app = create_app(orchestrator=build_fake_orchestrator(), callback_sender=RecordingCallbackSender(), events=events)
    return app, events


# ─────────────────────────────────────────────────────────────
# 스킬 서버가 응답마다 한 줄씩 남긴다


def test_버튼과_짧은_말과_사진도_기록한다():
    app, events = app_with_events()
    photo = kakao_request("https://talk.kakaocdn.net/photo.jpg")
    photo["userRequest"]["params"] = {"media": {"type": "image"}}
    with TestClient(app) as client:
        for body in (kakao_request("사용법"), kakao_request("주의사항"), kakao_request("안녕하세요"), photo):
            client.post("/skill", json=body)
    assert [(e.kind, e.outcome) for e in events.events] == [
        ("guide", None),
        ("caution", None),
        ("small_talk", "greeting"),
        ("media", "media"),
    ]
    assert all(e.user_hash == user_hash(USER) for e in events.events)


def test_채널_진입_인사는_보낸_메시지와_구분한다():
    app, events = app_with_events()
    body = kakao_request("")
    body["intent"]["name"] = "웰컴 블록"
    with TestClient(app) as client:
        client.post("/skill", json=body)
        client.post("/skill", json=kakao_request("처음"))
    assert [e.outcome for e in events.events] == ["intent", "command"]


def test_친구에게_보내기를_누르면_클릭을_세고_공유_카드를_보낸다():
    app, events = app_with_events()
    with TestClient(app) as client:
        app.state.last_results.put(USER, result(Label.CAUTION))
        payload = client.post("/skill", json=kakao_request("친구에게 보내기")).json()
    card = payload["template"]["outputs"][-1]["textCard"]
    assert card["buttons"] == [{"action": "share", "label": "친구에게 보내기"}]
    assert [(e.kind, e.outcome) for e in events.events] == [("share", "CAUTION")]


def test_직전_결과가_없으면_공유와_근거_보기는_none으로_남는다():
    app, events = app_with_events()
    with TestClient(app) as client:
        for text in ("친구에게 보내기", "왜 그런지 보기"):
            payload = client.post("/skill", json=kakao_request(text)).json()
            assert "먼저" in payload["template"]["outputs"][0]["simpleText"]["text"]
    assert [(e.kind, e.outcome) for e in events.events] == [("share", "none"), ("reason", "none")]


async def test_워커는_결과를_보낸_뒤_걸린_시간과_전송_여부를_남긴다():
    events = InMemoryEventLog()
    worker = Worker(InProcessJobQueue(), build_fake_orchestrator(), RecordingCallbackSender(), events=events)
    await worker.process(
        CheckJob(
            job_id="job-1",
            share_input=step1_input.classify("이 영상 보세요 https://youtu.be/blueberry01"),
            callback_url="https://callback.kakao.example/abc",
            user_key=USER,
        )
    )
    (event,) = events.events
    assert event.kind == "check"
    assert event.input_kind == "YOUTUBE" and event.input_form == "video+text" and event.site == "youtube.com"
    assert event.latency_ms is not None and event.delivered is True


async def test_콜백이_실패하면_보내지_못한_것으로_남긴다():
    events = InMemoryEventLog()
    worker = Worker(
        InProcessJobQueue(), build_fake_orchestrator(), RecordingCallbackSender(fail_on="kakao.example"), events=events
    )
    await worker.process(
        CheckJob(job_id="j", share_input=step1_input.classify("홍삼이 면역력에 좋다"),
                 callback_url="https://callback.kakao.example/abc", user_key=USER, via="try")
    )
    assert events.events[0].delivered is False
    assert events.events[0].via == "try"


def test_기록에는_보낸_글_내용이_없다():
    """분류(종류·형태·사이트)만 남긴다."""
    names = {field.name for field in fields(Event)}
    assert not names & {"raw_text", "text", "utterance", "url", "user_key"}
    event = new_event("check", user_key=USER, share_input=step1_input.classify("홍삼이 면역력에 좋다고 하네요"))
    assert "홍삼" not in repr(event)
    assert USER not in repr(event)


def test_익명_번호는_판정_기록의_만료_해시와_같다():
    """판정 기록은 30일 뒤 사용자 키를 이 방식으로 바꾼다. 같아야 이어서 셀 수 있다."""
    import hashlib

    assert user_hash(USER) == hashlib.sha256(USER.encode("utf-8")).hexdigest()[:32]


@pytest.mark.parametrize(
    "text, form, site",
    [
        ("https://www.youtube.com/shorts/abcdefghijk", "shorts", "youtube.com"),
        ("https://m.blog.naver.com/abc/123", "link", "blog.naver.com"),
        ("https://naver.me/F42RjeGj", "short_link", "naver.me"),
        ("홍삼", "short_text", None),
        ("홍삼이 면역력에 좋다고 합니다. " * 12, "long_text", None),
    ],
)
def test_입력_형태를_나눈다(text, form, site):
    assert describe_input(step1_input.classify(text)) == (form, site)


async def test_SQLite_기록은_다시_읽히고_90일이_지나면_지워진다(tmp_path):
    log = SqliteEventLog(tmp_path / "events.db")
    now = datetime.now(timezone.utc)
    await log.record(new_event("guide", user_key=USER, now=now - timedelta(days=91)))
    await log.record(new_event("check", user_key=USER, outcome="CAUTION", latency_ms=1200, delivered=True, now=now))
    assert [e.kind for e in await log.list_events()] == ["guide", "check"]
    assert await log.purge_expired(now) == 1
    (left,) = await log.list_events()
    assert left.outcome == "CAUTION" and left.delivered is True and left.latency_ms == 1200


# ─────────────────────────────────────────────────────────────
# KPI 계산

DAY0 = datetime(2026, 10, 5, 10, 0, tzinfo=KST)


def ev(user: str, kind: str, minutes: float = 0, *, day: int = 0, outcome: str | None = None, via: str | None = None,
       latency: int | None = None, form: str | None = None) -> Event:
    return Event(
        event_id=f"{user}-{kind}-{day}-{minutes}",
        created_at=DAY0 + timedelta(days=day, minutes=minutes),
        user_hash=user,
        kind=kind,
        outcome=outcome,
        input_kind="PLAIN_TEXT" if kind == "check" else None,
        input_form=form or ("text" if kind == "check" else None),
        via=via,
        latency_ms=latency,
        delivered=True if kind == "check" else None,
    )


def sample() -> list[Event]:
    return [
        # 팀원: 교육 전부터 썼다(판정 기록에만 남아 있음)
        ev("team", "check", 5, outcome="TRUSTWORTHY"),
        # A: 사용법 → 한번 해 보기(예시 확인은 단계에서 뺌) → 대상 아님 → 믿을 만해요. 3일째 다시 옴
        ev("a", "guide", 0),
        ev("a", "try", 1),
        ev("a", "check", 1.1, outcome="TRUSTWORTHY", via="try"),
        ev("a", "check", 3, outcome="notice:not_target", latency=900),
        ev("a", "check", 5, outcome="TRUSTWORTHY", latency=8000),
        ev("a", "share", 6, outcome="TRUSTWORTHY"),
        ev("a", "reason", 7, outcome="TRUSTWORTHY"),
        ev("a", "check", 0, day=3, outcome="CAUTION", latency=12000),
        # B: 처음부터 성공, 8일째 다시 옴
        ev("b", "check", 2, outcome="WARNING", latency=10000),
        ev("b", "check", 0, day=8, outcome="UNKNOWN", latency=9000),
        # C: 알 수 없어요 뒤 떠남
        ev("c", "welcome", 0, outcome="intent"),
        ev("c", "check", 1, outcome="UNKNOWN", latency=11000),
        # 교육 뒤에 처음 온 사람은 이 회차가 아니다
        ev("late", "check", 0, day=2, outcome="CAUTION"),
    ]


def report_for(now_day: int = 20) -> dict:
    return compute(
        sample(),
        cohort=parse_cohort("2026-10-05T09:00~2026-10-05T18:00"),
        prior_first_seen={"team": DAY0 - timedelta(days=20)},
        now=DAY0 + timedelta(days=now_day),
    )


def test_참여자는_교육_시간에_처음_쓴_사람이고_팀원은_빠진다():
    assert report_for()["participants"] == 3


def test_혼자_확인_성공은_예시_확인과_알_수_없어요를_세지_않는다():
    assert report_for()["success"]["users"] == 2  # a, b (c는 알 수 없어요뿐)


def test_첫_성공까지_단계는_어르신이_보낸_메시지만_센다():
    steps = report_for()["steps"]
    assert sorted(steps["values"]) == [1, 4]  # b: 1단계, a: 사용법·한번 해 보기·대상 아님·믿을 만해요
    assert steps["never"] == 1
    assert steps["paths"][0][0] in ("주의하세요", "사용법 → 한번 해 보기 → 확인 대상 아님 → 믿을 만해요")


def test_재사용률은_기간이_지난_사람만_분모로_잡는다():
    reuse = report_for()["reuse"]
    assert reuse["3일 뒤"] == {"measurable": 3, "reused": 1, "window": (3, 6)}
    assert reuse["1주 뒤"] == {"measurable": 3, "reused": 1, "window": (7, 13)}
    early = report_for(now_day=4)["reuse"]
    assert early["3일 뒤"]["measurable"] == 0


def test_막힌_응답과_막힌_뒤_이탈을_센다():
    blocked = report_for()["blocked"]
    assert blocked["count"] == 3  # a 대상 아님, b 알 수 없어요, c 알 수 없어요
    assert blocked["denominator"] == 6  # 어르신이 보낸 확인 요청 6건(예시 확인 제외)
    assert blocked["left_30m"] == 2  # b의 8일째 알 수 없어요, c
    assert blocked["left_for_good"] == 2


def test_공유와_근거_보기_클릭률():
    r = report_for()
    assert r["share"]["clicks"] == 1 and r["share"]["cards"] == 6  # 예시 확인 카드도 공유 버튼이 달린다
    assert r["reason"]["users"] == 1 and r["reason"]["card_users"] == 3


def test_이용량과_하루_평균():
    usage = report_for()["usage"]
    assert usage["checks_per_user"] == [3, 2, 1]
    # a: 첫날 행동 6개(예시 확인 제외) + 3일째 1개 → 2일에 7개
    assert usage["daily_actions"][0] == pytest.approx(3.5)


def test_공유된_카드가_열린_수는_참여자의_카드로_이어_센다():
    events = sample()
    card = next(e for e in events if e.user_hash == "b" and e.kind == "check")
    events += [
        Event(event_id="o1", created_at=DAY0 + timedelta(hours=1), user_hash=None, kind="share_open", ref=card.event_id),
        Event(event_id="o2", created_at=DAY0 + timedelta(hours=2), user_hash=None, kind="share_open", ref=card.event_id),
        Event(event_id="o3", created_at=DAY0 + timedelta(hours=2), user_hash=None, kind="share_open", ref="team-card"),
    ]
    report = compute(
        events,
        cohort=parse_cohort("2026-10-05T09:00~2026-10-05T18:00"),
        prior_first_seen={"team": DAY0 - timedelta(days=20)},
        now=DAY0 + timedelta(days=20),
    )
    assert report["share_open"] == {
        "opens": 2, "by_target": Counter({"chat": 2}), "cards_opened": 1, "cards": 6, "senders": 1
    }


def test_보고서를_한국어로_찍는다():
    text = format_report(report_for())
    for phrase in ("참여자 수 3명", "혼자 확인 성공률 67%", "재사용률", "걸러 낸 과장·위험 정보 2건", "공유된 결과 카드가 열린 수", "친구 공유", "하루 평균"):
        assert phrase in text


def test_교육_기간은_날짜만_줘도_끝날_하루를_포함한다():
    cohort = parse_cohort("2026-10-05~2026-10-06")
    assert cohort.contains(datetime(2026, 10, 6, 23, 0, tzinfo=KST))
    assert not cohort.contains(datetime(2026, 10, 7, 0, 1, tzinfo=KST))


# ─────────────────────────────────────────────────────────────
# 공유된 카드 사진 링크


def test_결과_카드_사진에_공유_추적_주소를_건다():
    from healthcheck.api.share import with_share_link
    from healthcheck.pipeline.step6_response import render

    payload = render(result(Label.CAUTION), image_base_url="https://example.com")
    linked = with_share_link(payload, "https://example.com", "a" * 32)
    card = linked["template"]["outputs"][0]["basicCard"]
    assert card["thumbnail"]["link"] == {"web": "https://example.com/s/" + "a" * 32}
    assert {"action": "share", "label": "친구에게 보내기"} in card["buttons"]


def test_공유된_카드를_누르면_기록하고_채널로_보낸다():
    app, events = app_with_events()
    app.state.channel_url = "https://pf.kakao.com/_abc"
    with TestClient(app) as client:
        moved = client.get("/s/" + "b" * 32, follow_redirects=False)
        home = client.get("/s/" + "b" * 32 + "?to=home", follow_redirects=False)
        bad = client.get("/s/../../etc", follow_redirects=False)
    assert moved.status_code == 302 and moved.headers["location"] == "https://pf.kakao.com/_abc/chat"
    assert home.headers["location"] == "https://pf.kakao.com/_abc"
    assert bad.status_code == 404
    assert [(e.kind, e.ref, e.outcome) for e in events.events] == [("share_open", "b" * 32, "chat"), ("share_open", "b" * 32, "home")]


def test_채널_주소가_없으면_안내_화면을_보여_준다():
    app, _ = app_with_events()
    app.state.channel_url = None
    with TestClient(app) as client:
        page = client.get("/s/" + "c" * 32)
    assert page.status_code == 200 and "AI 건강지킴이" in page.text


async def test_워커는_확인_기록의_id를_카드_사진_링크에_건다():
    sender = RecordingCallbackSender()
    events = InMemoryEventLog()
    worker = Worker(InProcessJobQueue(), build_fake_orchestrator(), sender, image_base_url="https://example.com", events=events)
    await worker.process(CheckJob(job_id="j", share_input=step1_input.classify("홍삼이 면역력에 좋다"),
                                  callback_url="https://callback.kakao.example/abc", user_key=USER))
    (sent,) = sender.sent
    (card,) = [o["basicCard"] for o in sent.payload["template"]["outputs"] if "basicCard" in o]
    assert card["thumbnail"]["link"]["web"] == "https://example.com/s/" + events.events[0].event_id


def test_처음_배포한_응답_기록_표에_ref_칸을_더한다(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE events (event_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, user_hash TEXT, kind TEXT NOT NULL,"
        " outcome TEXT, input_kind TEXT, input_form TEXT, site TEXT, via TEXT, latency_ms INTEGER, delivered INTEGER)"
    )
    connection.commit()
    connection.close()
    SqliteEventLog(path)
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(events)")}
    assert "ref" in columns


# ─────────────────────────────────────────────────────────────
# 카카오 공식 공유 기능: 누른 수(공유 페이지)와 보낸 수(전송 알림)

TOKEN = "d" * 32


def web_share_app(admin_key: str | None = "admin-secret"):
    from healthcheck.api.share import SHARE_CARDS

    SHARE_CARDS.clear()
    app, events = app_with_events()
    app.state.kakao_js_key = "js-key-123"
    app.state.kakao_admin_key = admin_key
    app.state.image_base_url = "https://example.com"
    return app, events


def test_공식_공유를_쓰면_친구에게_보내기가_공유_페이지를_연다():
    from healthcheck.api.share import SHARE_CARDS, with_share_link
    from healthcheck.pipeline.step6_response import render

    SHARE_CARDS.clear()
    payload = render(result(Label.WARNING), image_base_url="https://example.com")
    card = with_share_link(payload, "https://example.com", TOKEN, web_share=True, user_key=USER)["template"]["outputs"][0]["basicCard"]
    button = next(b for b in card["buttons"] if b["label"] == "친구에게 보내기")
    assert button == {"action": "webLink", "label": "친구에게 보내기", "webLinkUrl": "https://example.com/share/" + TOKEN}
    stored = SHARE_CARDS.get(TOKEN)
    assert stored["owner"] == USER and stored["label"] == "주의하세요" and stored["description"].endswith(DISCLAIMER)


def test_공유_페이지는_누른_것으로_기록하고_공유_화면을_띄운다():
    from healthcheck.api.share import SDK_INTEGRITY, SDK_URL, SHARE_CARDS

    app, events = web_share_app()
    SHARE_CARDS.put(TOKEN, {"title": "마늘이 암을 없앤다</script><script>alert(1)", "description": "설명",
                            "imageUrl": "https://example.com/static/verdict/v1/warning.png", "label": "주의하세요", "owner": USER})
    with TestClient(app) as client:
        page = client.get("/share/" + TOKEN)
    assert page.status_code == 200
    assert SDK_URL in page.text and SDK_INTEGRITY in page.text
    assert "Kakao.Share.sendDefault" in page.text and '"share_token": "' + TOKEN in page.text
    assert "</script><script>alert" not in page.text  # 카드 문구가 스크립트를 끊지 못한다
    assert [(e.kind, e.outcome, e.ref, e.user_hash) for e in events.events] == [("share", "WARNING", TOKEN, user_hash(USER))]


def test_JS_키가_없으면_공유_페이지를_열지_않는다():
    app, events = web_share_app()
    app.state.kakao_js_key = None
    with TestClient(app) as client:
        assert client.get("/share/" + TOKEN).status_code == 404
    assert events.events == []


def test_카카오_전송_알림을_보낸_것으로_기록한다():
    app, events = web_share_app()
    with TestClient(app) as client:
        ok = client.post("/kakao/share-callback", headers={"Authorization": "KakaoAK admin-secret"},
                         data={"CHAT_TYPE": "DirectChat", "HASH_CHAT_ID": "room-1", "share_token": TOKEN})
        forged = client.post("/kakao/share-callback", headers={"Authorization": "KakaoAK wrong"},
                             data={"CHAT_TYPE": "DirectChat", "share_token": TOKEN})
    assert ok.status_code == 200 and forged.status_code == 401
    (event,) = events.events
    assert (event.kind, event.outcome, event.ref) == ("share_sent", "DirectChat", TOKEN)
    assert event.site and "room-1" not in event.site  # 채팅방 해시를 한 번 더 해시한다


def test_KPI에_누른_수와_보낸_수가_나온다():
    events = sample()
    card = next(e for e in events if e.user_hash == "b" and e.kind == "check")
    events += [
        Event(event_id="s1", created_at=DAY0 + timedelta(minutes=3), user_hash="b", kind="share", outcome="WARNING", ref=card.event_id),
        Event(event_id="s2", created_at=DAY0 + timedelta(minutes=4), user_hash=None, kind="share_sent", outcome="MultiChat",
              ref=card.event_id, site="room"),
    ]
    report = compute(events, cohort=parse_cohort("2026-10-05T09:00~2026-10-05T18:00"),
                     prior_first_seen={"team": DAY0 - timedelta(days=20)}, now=DAY0 + timedelta(days=20))
    assert report["share"]["clicks"] == 2  # a의 시험 기간 버튼 + b의 공유 페이지
    assert report["share_sent"] == {"sent": 1, "cards": 1, "senders": 1, "rooms": 1, "by_chat": Counter({"MultiChat": 1})}
    assert "실제로 보냄 1회" in format_report(report) and "단체 채팅 1" in format_report(report)


def test_공유_메시지에_원본_보기와_채널_홈_버튼을_단다():
    from healthcheck.api.share import SHARE_CARDS

    app, _ = web_share_app()
    SHARE_CARDS.put(TOKEN, {"title": "홍삼", "description": "설명", "imageUrl": None, "label": "믿을 만해요",
                            "owner": USER, "source": "https://www.youtube.com/shorts/hxG2eLxzlU4"})
    with TestClient(app) as client:
        page = client.get("/share/" + TOKEN).text
    assert '"title": "원본 보기"' in page and '"title": "채널 홈"' in page
    assert "https://example.com/o/" + TOKEN in page and "https://example.com/s/" + TOKEN + "?to=home" in page


def test_원본_보기는_서명이_맞는_주소로만_보낸다():
    from healthcheck.api.share import source_link

    app, events = web_share_app()
    url = "https://www.youtube.com/shorts/hxG2eLxzlU4"
    good = source_link("https://example.com", TOKEN, url, "admin-secret").replace("https://example.com", "")
    with TestClient(app) as client:
        moved = client.get(good, follow_redirects=False)
        forged = client.get(f"/o/{TOKEN}?u=https://evil.example&s=" + "0" * 32, follow_redirects=False)
    assert moved.status_code == 302 and moved.headers["location"] == url
    assert forged.status_code == 404
    assert [(e.kind, e.outcome) for e in events.events] == [("share_open", "source")]


def test_원본이_없으면_나도_확인해_보기를_단다():
    from healthcheck.api.share import SHARE_CARDS

    app, _ = web_share_app()
    SHARE_CARDS.put(TOKEN, {"title": "홍삼이 면역력에 좋다", "description": "설명", "imageUrl": None, "label": "믿을 만해요",
                            "owner": USER, "source": None})
    with TestClient(app) as client:
        page = client.get("/share/" + TOKEN).text
    assert '"title": "나도 확인해 보기"' in page and '"title": "채널 홈"' in page


def test_공유_페이지는_보낸_뒤에_스스로_닫힌다():
    import time as _time

    app, _ = web_share_app()
    with TestClient(app) as client:
        before = int(_time.time() * 1000)
        assert client.get(f"/share/{TOKEN}/sent?after={before}").json() == {"sent": False}
        client.post("/kakao/share-callback", headers={"Authorization": "KakaoAK admin-secret"},
                    data={"CHAT_TYPE": "MemoChat", "share_token": TOKEN})
        assert client.get(f"/share/{TOKEN}/sent?after={before}").json() == {"sent": True}
        # 이전에 보낸 기록으로 새로 연 페이지를 닫지 않는다
        later = int(_time.time() * 1000) + 5000
        assert client.get(f"/share/{TOKEN}/sent?after={later}").json() == {"sent": False}
        page = client.get("/share/" + TOKEN).text
    assert "kakaotalk://inappbrowser/close" in page and "/sent" in page


def test_포스터_QR은_스캔을_세고_채널_홈으로_보낸다():
    app, events = app_with_events()
    app.state.channel_url = "https://pf.kakao.com/_abc"
    with TestClient(app) as client:
        moved = client.get("/q/poster", follow_redirects=False)
        bad = client.get("/q/Poster!!", follow_redirects=False)
    assert moved.status_code == 302 and moved.headers["location"] == "https://pf.kakao.com/_abc"
    assert bad.status_code == 404
    assert [(e.kind, e.outcome, e.user_hash) for e in events.events] == [("poster_scan", "poster", None)]


def test_KPI에_포스터_스캔이_장소별로_나온다():
    events = sample() + [
        Event(event_id="p1", created_at=DAY0 + timedelta(minutes=1), user_hash=None, kind="poster_scan", outcome="poster-a"),
        Event(event_id="p2", created_at=DAY0 + timedelta(minutes=2), user_hash=None, kind="poster_scan", outcome="poster-a"),
        Event(event_id="p3", created_at=DAY0 + timedelta(days=3), user_hash=None, kind="poster_scan", outcome="poster-b"),
    ]
    report = compute(events, cohort=parse_cohort("2026-10-05T09:00~2026-10-05T18:00"),
                     prior_first_seen={"team": DAY0 - timedelta(days=20)}, now=DAY0 + timedelta(days=20))
    assert report["poster"]["scans"] == 2 and report["poster"]["by_tag"] == Counter({"poster-a": 2})
    assert "포스터 QR 스캔 2회" in format_report(report)
