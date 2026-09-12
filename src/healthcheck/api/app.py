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
from healthcheck.adapters.liner.academic import LinerAcademicSearchClient
from healthcheck.adapters.fake.cache import InMemoryExtractionCache, InMemoryVerdictCache
from healthcheck.adapters.fake.callback import RecordingCallbackSender
from healthcheck.adapters.fake.mfds import FakeMfdsDataSource
from healthcheck.adapters.fake.transcript import FakeTranscriptExtractor
from healthcheck.adapters.inprocess_queue import InProcessJobQueue
from healthcheck.api import skill
from healthcheck.config import Settings, load_settings
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


def build_orchestrator(settings: Settings | None = None) -> Orchestrator:
    """설정에 따라 실제 어댑터와 Fake를 골라 꽂는다.

    어댑터를 하나씩 독립적으로 바꾼다. Liner 키는 있고 식약처 키는 아직
    없는 지금 상황에서, 학술 검증만 실제로 돌리고 식약처는 Fake로 두는
    조합이 가능해야 한다.
    """
    settings = settings or load_settings()
    live = settings.adapter_mode == "live"

    if live and settings.has_liner_key:
        academic = LinerAcademicSearchClient(settings.liner_api_key)
        logger.info("학술 검증: Liner 실제 호출")
    else:
        academic = FakeAcademicSearchClient()
        if live:
            logger.warning("ADAPTER_MODE=live이지만 LINER_API_KEY가 없어 Fake를 씁니다")

    # TODO(확인필요): 식약처 오픈API 인증키가 아직 없다. 키가 나오면
    # MfdsDataSource 실제 구현을 만들어 여기에 꽂는다.
    if live and settings.has_mfds_key:
        logger.warning("식약처 실제 구현이 아직 없어 Fake를 씁니다")
    mfds = FakeMfdsDataSource()

    # 자막 추출기도 실제 구현이 없다. 자막 확보 방식을 정한 뒤 만든다.
    return Orchestrator(
        transcript=FakeTranscriptExtractor(),
        academic=academic,
        mfds=mfds,
        claim_extractor=RuleBasedClaimExtractor(),
        ruleset=load_ruleset(settings.rules_path),
        verdict_cache=InMemoryVerdictCache(),
        extraction_cache=InMemoryExtractionCache(),
    )


def create_app(
    orchestrator: Orchestrator | None = None,
    callback_sender=None,
    *,
    worker_count: int = 2,
) -> FastAPI:
    orchestrator = orchestrator or build_orchestrator()
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
