"""저장소와 보존 기간 정책 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from healthcheck.api.app import build_fake_orchestrator, create_app
from healthcheck.domain.labels import Label
from healthcheck.pipeline import step1_input
from healthcheck.storage.memory_repo import InMemoryCheckRepository
from healthcheck.storage.record import build_record
from healthcheck.storage.sqlite_repo import SqliteCheckRepository

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


@pytest.fixture(params=["sqlite", "memory"])
def repository(request, tmp_path):
    """두 구현이 같은 계약을 지키는지 함께 검증한다."""
    if request.param == "sqlite":
        repo = SqliteCheckRepository(tmp_path / "test.db")
        yield repo
        repo.close()
    else:
        yield InMemoryCheckRepository()


async def record_for(text: str, user_key: str | None = "botuser-1", now=NOW):
    orchestrator = build_fake_orchestrator()
    share_input = step1_input.classify(text)
    result = await orchestrator.run(share_input)
    return build_record(share_input=share_input, result=result, user_key=user_key, now=now)


# ── 저장과 지표 ──────────────────────────────────────────────


async def test_판정_기록을_저장한다(repository):
    await repository.save(await record_for("냉동 블루베리가 당뇨를 완치시킨다"))

    metrics = await repository.metrics()
    assert metrics.total_checks == 1
    assert metrics.by_label[Label.WARNING.value] == 1


async def test_규칙_버전별로_집계한다(repository):
    """규칙표가 바뀌면 이전 판정과 섞이지 않게 구분해야 한다."""
    await repository.save(await record_for("홍삼이 면역력 증진에 도움을 줄 수 있다"))

    metrics = await repository.metrics()
    assert sum(metrics.rules_versions.values()) == 1


async def test_오정보_유형_수를_센다(repository):
    """발표 자료의 목표 성과 지표 중 하나다."""
    await repository.save(await record_for("냉동 블루베리가 당뇨를 완치시킨다"))
    await repository.save(await record_for("냉동 블루베리가 당뇨를 완치시킨다"))
    await repository.save(await record_for("마늘이 혈압을 낮추는 데 좋다고 합니다"))

    metrics = await repository.metrics()
    # 같은 (규칙, 성분) 조합은 한 유형으로 센다.
    assert metrics.total_checks == 3
    assert metrics.misinformation_types == 2


async def test_참여자_수는_사용자_키로_센다(repository):
    await repository.save(await record_for("홍삼이 면역력에 좋다", user_key="a"))
    await repository.save(await record_for("루테인이 눈 건강에 좋다", user_key="a"))
    await repository.save(await record_for("마늘이 혈압에 좋다", user_key="b"))

    assert (await repository.metrics()).distinct_users == 2


# ── 보존 기간 정책 ───────────────────────────────────────────


async def test_보존_기간이_지난_원문을_비운다(repository):
    """원문과 자막은 90일만 둔다."""
    record = await record_for("냉동 블루베리가 당뇨를 완치시킨다")
    await repository.save(record)

    purged = await repository.purge_expired(now=NOW + timedelta(days=91))

    assert purged["texts_cleared"] == 1
    # 판정 기록은 남는다. 지표 집계가 유지되어야 한다.
    assert (await repository.metrics()).total_checks == 1


async def test_보존_기간_안에는_비우지_않는다(repository):
    await repository.save(await record_for("홍삼이 면역력에 좋다"))

    purged = await repository.purge_expired(now=NOW + timedelta(days=10))

    assert purged["texts_cleared"] == 0
    assert purged["user_keys_hashed"] == 0


async def test_사용자_키는_지우지_않고_해시로_바꾼다(repository):
    """같은 사람인지는 구분할 수 있지만 누구인지는 알 수 없게 한다."""
    await repository.save(await record_for("홍삼이 면역력에 좋다", user_key="botuser-secret"))

    purged = await repository.purge_expired(now=NOW + timedelta(days=31))

    assert purged["user_keys_hashed"] == 1
    # 참여자 수는 여전히 셀 수 있다.
    assert (await repository.metrics()).distinct_users == 1


async def test_사용자_키가_없으면_해시할_것도_없다(repository):
    await repository.save(await record_for("홍삼이 면역력에 좋다", user_key=None))

    purged = await repository.purge_expired(now=NOW + timedelta(days=365))

    assert purged["user_keys_hashed"] == 0


async def test_정리를_두_번_돌려도_같은_결과다(repository):
    """하루 한 번 돌리는 스크립트다. 여러 번 돌아도 안전해야 한다."""
    await repository.save(await record_for("홍삼이 면역력에 좋다"))

    first = await repository.purge_expired(now=NOW + timedelta(days=200))
    second = await repository.purge_expired(now=NOW + timedelta(days=200))

    assert first["texts_cleared"] == 1
    assert second["texts_cleared"] == 0
    assert second["user_keys_hashed"] == 0


# ── 재시작 후에도 남는다 ─────────────────────────────────────


async def test_SQLite는_재시작해도_기록이_남는다(tmp_path):
    """메모리 저장소로는 목표 지표를 집계할 수 없다."""
    path = tmp_path / "persist.db"
    first = SqliteCheckRepository(path)
    await first.save(await record_for("냉동 블루베리가 당뇨를 완치시킨다"))
    first.close()

    second = SqliteCheckRepository(path)
    assert (await second.metrics()).total_checks == 1
    second.close()


# ── 파이프라인 연결 ──────────────────────────────────────────


async def test_워커가_판정을_기록한다():
    from healthcheck.adapters.fake.callback import RecordingCallbackSender
    from healthcheck.adapters.inprocess_queue import InProcessJobQueue
    from healthcheck.api.worker import Worker
    from healthcheck.ports.queue import CheckJob

    repository = InMemoryCheckRepository()
    worker = Worker(
        InProcessJobQueue(), build_fake_orchestrator(), RecordingCallbackSender(), repository
    )
    job = CheckJob(
        job_id="job-1",
        share_input=step1_input.classify("https://youtu.be/blueberry01"),
        callback_url="https://callback.kakao.example/abc",
        user_key="botuser-1",
    )

    await worker.process(job)

    assert len(repository.records) == 1
    assert repository.records[0].user_key == "botuser-1"


async def test_콜백이_실패해도_기록은_남는다():
    """판정은 일어난 일이다. 전송 실패와 별개로 지표에 들어가야 한다."""
    from healthcheck.adapters.fake.callback import RecordingCallbackSender
    from healthcheck.adapters.inprocess_queue import InProcessJobQueue
    from healthcheck.api.worker import Worker
    from healthcheck.ports.queue import CheckJob

    repository = InMemoryCheckRepository()
    worker = Worker(
        InProcessJobQueue(),
        build_fake_orchestrator(),
        RecordingCallbackSender(fail_on="kakao.example"),
        repository,
    )

    await worker.process(
        CheckJob(
            job_id="job-1",
            share_input=step1_input.classify("홍삼이 면역력에 좋다"),
            callback_url="https://callback.kakao.example/abc",
        )
    )

    assert len(repository.records) == 1


def test_metrics_엔드포인트가_지표를_돌려준다():
    from fastapi.testclient import TestClient

    repository = InMemoryCheckRepository()
    app = create_app(build_fake_orchestrator(), worker_count=0, repository=repository)

    with TestClient(app) as client:
        body = client.get("/metrics").json()

    assert body["누적_확인_건수"] == 0
    assert "오정보_유형_수" in body


def test_metrics에_개인정보가_없다():
    """지표는 집계값만 담는다."""
    from fastapi.testclient import TestClient

    app = create_app(build_fake_orchestrator(), worker_count=0)
    with TestClient(app) as client:
        body = client.get("/metrics").json()

    assert "user_key" not in str(body)
    assert "raw_text" not in str(body)


async def test_성과_공유회용_집계를_함께_낸다(repository):
    """입력 종류·날짜별 건수·많이 확인한 성분. 개인을 식별하는 값은 담지 않는다."""
    await repository.save(await record_for("홍삼이 면역력 증진에 도움을 줄 수 있다", now=NOW))
    await repository.save(await record_for("https://youtu.be/redginseng1", now=NOW))
    await repository.save(await record_for("마늘이 혈압을 낮추는 데 좋다", now=NOW + timedelta(days=1)))

    metrics = await repository.metrics()
    assert metrics.by_input_kind == {"PLAIN_TEXT": 2, "YOUTUBE": 1}
    assert metrics.daily == {"2026-09-12": 2, "2026-09-13": 1}
    assert dict(metrics.top_ingredients)["홍삼"] >= 1
