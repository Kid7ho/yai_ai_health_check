"""FastAPI 앱 조립.

외부 의존성은 전부 포트 뒤에 있으므로, 여기서 무엇을 꽂을지만 정한다.
API 키가 모두 승인 대기 중이라 기본값은 Fake다. 키가 준비되면 이 함수만
바꾸면 되고 파이프라인 코드는 건드리지 않는다.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from healthcheck.adapters.fake.academic import FakeAcademicSearchClient
from healthcheck.adapters.fake.cache import InMemoryExtractionCache, InMemoryVerdictCache
from healthcheck.adapters.fake.callback import RecordingCallbackSender
from healthcheck.adapters.fake.mfds import FakeMfdsDataSource
from healthcheck.adapters.fake.transcript import FakeTranscriptExtractor
from healthcheck.adapters.inprocess_queue import InProcessJobQueue
from healthcheck.api import skill
from healthcheck.api.worker import start_workers
from healthcheck.pipeline.orchestrator import Orchestrator
from healthcheck.pipeline.step3_claims import RuleBasedClaimExtractor
from healthcheck.pipeline.step5_verdict import load_ruleset

logger = logging.getLogger(__name__)


def build_fake_orchestrator() -> Orchestrator:
    """키 없이 전체 파이프라인이 끝까지 돌아가는 조립."""
    return Orchestrator(
        transcript=FakeTranscriptExtractor(),
        academic=FakeAcademicSearchClient(),
        mfds=FakeMfdsDataSource(),
        claim_extractor=RuleBasedClaimExtractor(),
        ruleset=load_ruleset(),
        verdict_cache=InMemoryVerdictCache(),
        extraction_cache=InMemoryExtractionCache(),
    )


def create_app(
    orchestrator: Orchestrator | None = None,
    callback_sender=None,
    *,
    worker_count: int = 2,
) -> FastAPI:
    orchestrator = orchestrator or build_fake_orchestrator()
    callback_sender = callback_sender or RecordingCallbackSender()
    queue = InProcessJobQueue()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = await start_workers(queue, orchestrator, callback_sender, count=worker_count)
        logger.info("워커 %d개 기동. 규칙표 %s", worker_count, orchestrator.ruleset.version)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()

    app = FastAPI(title="건강정보 확인", lifespan=lifespan)
    app.state.services = skill.SkillServices(orchestrator=orchestrator, queue=queue)
    app.state.queue = queue
    app.state.callback_sender = callback_sender
    app.include_router(skill.router)

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "rules_version": orchestrator.ruleset.version,
            "queued": queue.size,
        }

    return app


app = create_app()
