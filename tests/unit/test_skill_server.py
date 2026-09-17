"""카카오 스킬 서버 골격 테스트.

검증하는 것은 세 가지다.
  1. 요청을 받으면 5초 안에 즉시 ack를 돌려주고 외부 호출을 하지 않는다
  2. 워커가 파이프라인을 돌려 콜백으로 결과를 보낸다
  3. 캐시가 맞으면 동기로 답한다 (예외 경로)
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from healthcheck.adapters.fake.callback import RecordingCallbackSender
from healthcheck.adapters.fake.transcript import FakeTranscriptExtractor
from healthcheck.adapters.inprocess_queue import InProcessJobQueue, QueueFull
from healthcheck.api.app import build_fake_orchestrator, create_app
from healthcheck.api.worker import Worker
from healthcheck.domain.labels import Label
from healthcheck.pipeline import step1_input
from healthcheck.pipeline.step6_response import DISCLAIMER
from healthcheck.ports.queue import CheckJob

CALLBACK_URL = "https://callback.kakao.example/abc123"
BLUEBERRY_URL = "https://youtu.be/blueberry01"
GINSENG_URL = "https://youtu.be/redginseng1"


def kakao_request(utterance: str, *, callback_url: str | None = CALLBACK_URL) -> dict:
    """카카오 스킬 요청 본문. 확인된 필드 경로만 쓴다."""
    body = {
        "bot": {"id": "bot-1", "name": "건강정보 확인"},
        "intent": {"id": "block-1", "name": "폴백 블록"},
        "userRequest": {
            "utterance": utterance,
            "user": {
                "id": "botuserkey-abc",
                "type": "botUserKey",
                "properties": {"isFriend": True},
            },
            "lang": "ko",
            "timezone": "Asia/Seoul",
        },
        "action": {"id": "skill-1", "name": "확인", "params": {}},
    }
    if callback_url is not None:
        body["userRequest"]["callbackUrl"] = callback_url
    return body


def texts_of(payload: dict) -> list[str]:
    # 면책 문구가 카드 안에 붙으므로 카드 설명도 함께 본다.
    return [
        output.get("simpleText", {}).get("text")
        or output.get("textCard", {}).get("description")
        or output.get("basicCard", {}).get("description", "")
        for output in payload.get("template", {}).get("outputs", [])
    ]


# ─────────────────────────────────────────────────────────────
# 즉시 ack — 5초 제한을 지키는 기본 경로
# ─────────────────────────────────────────────────────────────


def test_요청을_받으면_즉시_ack를_돌려준다():
    app = create_app(worker_count=0)
    with TestClient(app) as client:
        response = client.post("/skill", json=kakao_request(BLUEBERRY_URL))

    assert response.status_code == 200
    body = response.json()
    assert body["useCallback"] is True
    assert "template" not in body


def test_ack_경로에서는_외부_호출을_하지_않는다():
    """자막을 가져오는 순간 5초를 보장할 수 없다."""
    orchestrator = build_fake_orchestrator()
    transcript: FakeTranscriptExtractor = orchestrator.transcript  # type: ignore[assignment]

    app = create_app(orchestrator, worker_count=0)
    with TestClient(app) as client:
        client.post("/skill", json=kakao_request(BLUEBERRY_URL))

    assert transcript.calls == []


def test_요청이_큐에_쌓인다():
    app = create_app(worker_count=0)
    with TestClient(app) as client:
        client.post("/skill", json=kakao_request(BLUEBERRY_URL))
        assert app.state.queue.size == 1


def test_콜백_URL이_없으면_안내_문구를_준다():
    """오픈빌더에서 콜백 사용 설정을 켜지 않으면 callbackUrl이 오지 않는다."""
    app = create_app(worker_count=0)
    with TestClient(app) as client:
        body = client.post(
            "/skill", json=kakao_request(BLUEBERRY_URL, callback_url=None)
        ).json()

    assert "잠시 뒤에 다시" in texts_of(body)[0]
    assert app.state.queue.size == 0


def test_큐가_꽉_차면_안내_문구를_준다():
    class FullQueue:
        async def enqueue(self, job: CheckJob) -> None:
            raise QueueFull("꽉 찼다")

        async def dequeue(self) -> CheckJob:  # pragma: no cover
            raise NotImplementedError

    app = create_app(worker_count=0)
    with TestClient(app) as client:
        app.state.services.queue = FullQueue()
        body = client.post("/skill", json=kakao_request(BLUEBERRY_URL)).json()

    assert "요청이 많아요" in texts_of(body)[0]


def test_요청_본문이_비어_있어도_500을_내지_않는다():
    """5초 안에 응답하지 못하면 어르신 화면에는 아무 답도 안 뜬다."""
    app = create_app(worker_count=0)
    with TestClient(app) as client:
        response = client.post("/skill", json={})

    assert response.status_code == 200
    assert "잠시 뒤에 다시" in texts_of(response.json())[0]


def test_health_에_규칙표_버전이_나온다():
    app = create_app(worker_count=0)
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["rules_version"] == "v0.13"


# ─────────────────────────────────────────────────────────────
# 워커 — 파이프라인 실행과 콜백 전송
# ─────────────────────────────────────────────────────────────


def job_for(url: str) -> CheckJob:
    return CheckJob(
        job_id="job-1",
        share_input=step1_input.classify(url),
        callback_url=CALLBACK_URL,
        user_key="botuserkey-abc",
    )


async def test_워커가_판정_결과를_콜백으로_보낸다():
    sender = RecordingCallbackSender()
    worker = Worker(InProcessJobQueue(), build_fake_orchestrator(), sender)

    await worker.process(job_for(GINSENG_URL))

    assert sender.last is not None
    assert sender.last.callback_url == CALLBACK_URL
    card = sender.last.payload["template"]["outputs"][0]["textCard"]
    assert card["title"] == "믿을 만해요"


async def test_콜백_응답에도_면책_문구가_들어간다():
    sender = RecordingCallbackSender()
    worker = Worker(InProcessJobQueue(), build_fake_orchestrator(), sender)

    await worker.process(job_for(BLUEBERRY_URL))

    assert any(DISCLAIMER in t for t in texts_of(sender.last.payload))


async def test_질병_완치_주장은_주의로_콜백된다():
    sender = RecordingCallbackSender()
    worker = Worker(InProcessJobQueue(), build_fake_orchestrator(), sender)

    await worker.process(job_for(BLUEBERRY_URL))

    card = sender.last.payload["template"]["outputs"][0]["textCard"]
    assert card["title"] == "주의하세요"


async def test_자막이_없으면_알_수_없어요로_콜백된다():
    """생성형 AI 제작 영상은 자막이 없는 경우가 많다."""
    sender = RecordingCallbackSender()
    worker = Worker(InProcessJobQueue(), build_fake_orchestrator(), sender)

    await worker.process(job_for("https://youtu.be/nosuchvideo"))

    card = sender.last.payload["template"]["outputs"][0]["textCard"]
    assert card["title"] == "알 수 없어요"


async def test_범위_밖_요청은_거절_안내로_콜백된다():
    sender = RecordingCallbackSender()
    worker = Worker(InProcessJobQueue(), build_fake_orchestrator(), sender)

    await worker.process(job_for("https://youtu.be/outofscope1"))

    texts = texts_of(sender.last.payload)
    assert "의사" in texts[0]
    assert any(DISCLAIMER in t for t in texts)


async def test_파이프라인이_실패해도_콜백은_한_번_보낸다():
    """콜백은 유효 5분에 1회다. 아무것도 안 보내면 화면에 안내만 남는다."""

    class BrokenOrchestrator:
        ruleset = build_fake_orchestrator().ruleset

        async def cached(self, share_input):
            return None

        async def run(self, share_input):
            raise RuntimeError("자막 서버가 죽었다")

    sender = RecordingCallbackSender()
    worker = Worker(InProcessJobQueue(), BrokenOrchestrator(), sender)

    await worker.process(job_for(GINSENG_URL))

    assert len(sender.sent) == 1
    card = sender.last.payload["template"]["outputs"][0]["textCard"]
    assert card["title"] == "알 수 없어요"


async def test_콜백_전송이_실패해도_워커가_죽지_않는다():
    sender = RecordingCallbackSender(fail_on="callback.kakao.example")
    worker = Worker(InProcessJobQueue(), build_fake_orchestrator(), sender)

    await worker.process(job_for(GINSENG_URL))  # 예외가 밖으로 나오지 않는다

    assert sender.sent == []


async def test_큐에_넣은_작업이_워커를_거쳐_처리된다():
    queue = InProcessJobQueue()
    sender = RecordingCallbackSender()
    worker = Worker(queue, build_fake_orchestrator(), sender)

    task = asyncio.create_task(worker.run_forever())
    await queue.enqueue(job_for(GINSENG_URL))
    await queue.join()
    task.cancel()

    assert len(sender.sent) == 1


# ─────────────────────────────────────────────────────────────
# 캐시 적중 — 5초 동기 응답 예외 경로
# ─────────────────────────────────────────────────────────────


async def test_같은_영상을_다시_보내면_동기로_답한다():
    """경로당에서 같은 영상이 여러 어르신에게 돈다. 적중률이 높다."""
    orchestrator = build_fake_orchestrator()
    transcript: FakeTranscriptExtractor = orchestrator.transcript  # type: ignore[assignment]

    # 첫 요청은 파이프라인을 돈다.
    first = await orchestrator.run(step1_input.classify(GINSENG_URL))
    assert first.from_cache is False
    calls_after_first = len(transcript.calls)

    # 두 번째는 캐시에서 나온다. 외부 호출이 늘지 않는다.
    cached = await orchestrator.cached(step1_input.classify(GINSENG_URL))
    assert cached is not None
    assert cached.from_cache is True
    assert cached.verdict.label is Label.TRUSTWORTHY
    assert len(transcript.calls) == calls_after_first


async def test_주소_형태가_달라도_캐시가_맞는다():
    orchestrator = build_fake_orchestrator()
    await orchestrator.run(step1_input.classify("https://youtu.be/redginseng1"))

    other = step1_input.classify("https://www.youtube.com/watch?v=redginseng1&t=30s")
    assert await orchestrator.cached(other) is not None


async def test_규칙표_버전이_바뀌면_판정_캐시가_무효화된다():
    """규칙표가 바뀌면 과거 판정은 더 이상 유효하지 않다."""
    from dataclasses import replace

    orchestrator = build_fake_orchestrator()
    share_input = step1_input.classify(GINSENG_URL)
    await orchestrator.run(share_input)

    orchestrator.ruleset = replace(orchestrator.ruleset, version="v1")
    assert await orchestrator.cached(share_input) is None


async def test_추출_캐시는_규칙표_버전과_무관하게_유지된다():
    from dataclasses import replace

    orchestrator = build_fake_orchestrator()
    share_input = step1_input.classify(GINSENG_URL)
    await orchestrator.run(share_input)

    extraction_cache = orchestrator.extraction_cache
    hits_before = extraction_cache.hits

    orchestrator.ruleset = replace(orchestrator.ruleset, version="v1")
    await orchestrator.run(share_input)

    assert extraction_cache.hits == hits_before + 1


def test_캐시가_맞으면_스킬_엔드포인트가_동기로_답한다():
    orchestrator = build_fake_orchestrator()
    asyncio.run(orchestrator.run(step1_input.classify(GINSENG_URL)))

    app = create_app(orchestrator, worker_count=0)
    with TestClient(app) as client:
        body = client.post("/skill", json=kakao_request(GINSENG_URL)).json()

    assert "useCallback" not in body
    assert body["template"]["outputs"][0]["textCard"]["title"] == "믿을 만해요"
    assert any(DISCLAIMER in t for t in texts_of(body))
    assert app.state.queue.size == 0


# ─────────────────────────────────────────────────────────────
# 불필요한 외부 조회 차단 — Liner 토큰 비용
# ─────────────────────────────────────────────────────────────


async def test_자막_신뢰도가_낮으면_학술_검색을_하지_않는다():
    """R-010이 판정을 보류할 입력에 토큰을 쓰지 않는다."""
    orchestrator = build_fake_orchestrator()
    academic = orchestrator.academic

    result = await orchestrator.run(step1_input.classify("https://youtu.be/lowconf0001"))

    assert result.verdict.rule_id == "R-010"
    assert academic.calls == []


async def test_범위_밖_입력에는_학술_검색을_하지_않는다():
    orchestrator = build_fake_orchestrator()
    academic = orchestrator.academic

    result = await orchestrator.run(step1_input.classify("https://youtu.be/outofscope1"))

    assert result.verdict.rule_id == "R-001"
    assert academic.calls == []


async def test_주장을_못_뽑으면_학술_검색을_하지_않는다():
    orchestrator = build_fake_orchestrator()
    academic = orchestrator.academic

    await orchestrator.run(step1_input.classify("https://youtu.be/nosuchvideo"))

    assert academic.calls == []


async def test_정상_입력에는_학술_검색을_한다():
    orchestrator = build_fake_orchestrator()
    academic = orchestrator.academic

    await orchestrator.run(step1_input.classify(GINSENG_URL))

    assert len(academic.calls) == 1


# ─────────────────────────────────────────────────────────────
# 자막 서버 차단 — 일시적 실패는 캐시하지 않는다
# ─────────────────────────────────────────────────────────────


async def test_자막_서버_차단은_판정을_캐시하지_않는다():
    """차단 때문에 나온 '알 수 없어요'를 캐시하면 차단이 풀려도 같은 답을 준다."""
    from healthcheck.adapters.youtube.transcript import TranscriptFetchError
    from healthcheck.domain.labels import Label as _Label

    class BlockedExtractor:
        async def fetch(self, video_id: str):
            raise TranscriptFetchError("YouTube가 IP를 차단했다")

    orchestrator = build_fake_orchestrator()
    orchestrator.transcript = BlockedExtractor()
    share_input = step1_input.classify(GINSENG_URL)

    result = await orchestrator.run(share_input)

    assert result.verdict.label is _Label.UNKNOWN
    assert result.verdict.rule_id == "R-010"
    assert await orchestrator.cached(share_input) is None


async def test_자막이_정말_없는_경우는_캐시한다():
    """없는 자막은 다시 시도해도 없다. 캐시해서 호출을 줄인다."""
    orchestrator = build_fake_orchestrator()
    share_input = step1_input.classify("https://youtu.be/nosuchvideo")

    await orchestrator.run(share_input)

    assert await orchestrator.cached(share_input) is not None


async def test_차단_상태를_health에_노출한다():
    """배포 후 가장 먼저 깨지는 지점이라 외부에서 확인할 수 있어야 한다."""
    from healthcheck.adapters.youtube.transcript import YouTubeTranscriptExtractor

    orchestrator = build_fake_orchestrator()
    extractor = YouTubeTranscriptExtractor(api=object(), min_interval=0, cooldown=600)
    extractor._breaker.trip()
    orchestrator.transcript = extractor

    app = create_app(orchestrator, worker_count=0)
    with TestClient(app) as client:
        body = client.get("/health").json()

    assert body["transcript_blocked"] is True
    assert body["status"] == "degraded"


async def test_차단된_영상은_다시_보내_달라고_안내한다():
    from healthcheck.adapters.youtube.transcript import TranscriptFetchError

    class BlockedExtractor:
        async def fetch(self, video_id: str):
            raise TranscriptFetchError("차단")

    sender = RecordingCallbackSender()
    orchestrator = build_fake_orchestrator()
    orchestrator.transcript = BlockedExtractor()
    worker = Worker(InProcessJobQueue(), orchestrator, sender)

    await worker.process(job_for(GINSENG_URL))

    assert "잠시 뒤에 다시 보내" in texts_of(sender.last.payload)[0]
