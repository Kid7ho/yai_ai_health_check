"""응답 기록과 교육 회차 KPI 테스트.

교육 성과(참여자 수, 성공률, 첫 성공까지 단계, 재사용률, 막힌 뒤 이탈, 공유 클릭)를 서버가
자동으로 세는지 본다. 보낸 글 내용은 기록에 남지 않아야 한다.
"""

from __future__ import annotations

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


def test_보고서를_한국어로_찍는다():
    text = format_report(report_for())
    for phrase in ("참여자 수 3명", "혼자 확인 성공률 67%", "재사용률", "걸러 낸 과장·위험 정보 2건", "친구 공유", "하루 평균"):
        assert phrase in text


def test_교육_기간은_날짜만_줘도_끝날_하루를_포함한다():
    cohort = parse_cohort("2026-10-05~2026-10-06")
    assert cohort.contains(datetime(2026, 10, 6, 23, 0, tzinfo=KST))
    assert not cohort.contains(datetime(2026, 10, 7, 0, 1, tzinfo=KST))
