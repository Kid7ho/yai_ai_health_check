"""FastAPI 앱 조립.

외부 의존성은 전부 포트 뒤에 있으므로, 여기서 무엇을 꽂을지만 정한다.
API 키가 모두 승인 대기 중이라 기본값은 Fake다. 키가 준비되면 이 함수만
바꾸면 되고 파이프라인 코드는 건드리지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from healthcheck.adapters.fake.academic import FakeAcademicSearchClient
from healthcheck.adapters.liner.academic import LinerAcademicSearchClient
from healthcheck.adapters.mfds.foodsafetykorea import FoodSafetyKoreaDataSource
from healthcheck.adapters.web.article import TrafilaturaArticleExtractor
from healthcheck.adapters.web.links import HttpLinkResolver
from healthcheck.adapters.youtube.asr import WhisperAsrTranscriber
from healthcheck.adapters.youtube.fallback import CaptionsThenAsrExtractor
from healthcheck.adapters.youtube.gemini import GeminiVideoTranscriber
from healthcheck.adapters.youtube.titled import TitledTranscriptExtractor, YouTubeTitleFetcher
from healthcheck.adapters.youtube.transcript import YouTubeTranscriptExtractor
from healthcheck.adapters.fake.article import FakeArticleExtractor
from healthcheck.adapters.fake.cache import InMemoryExtractionCache, InMemoryVerdictCache
from healthcheck.adapters.fake.links import FakeLinkResolver
from healthcheck.adapters.fake.callback import RecordingCallbackSender
from healthcheck.adapters.kakao.callback import HttpCallbackSender
from healthcheck.adapters.fake.mfds import FakeMfdsDataSource
from healthcheck.adapters.fake.transcript import FakeTranscriptExtractor
from healthcheck.adapters.inprocess_queue import InProcessJobQueue
from healthcheck.api import debug, skill
from healthcheck.api.auth import SkillAuth, warn_if_unprotected
from healthcheck.config import Settings, load_settings, setup_logging
from healthcheck.domain.labels import Label
from healthcheck.pipeline.step6_response import IMAGE_FILES, IMAGE_VERSION, verdict_image_url
from healthcheck.api.worker import start_workers
from healthcheck.pipeline.orchestrator import Orchestrator
from healthcheck.pipeline.step3_claims import RuleBasedClaimExtractor
from healthcheck.pipeline.step5_verdict import load_ruleset
from healthcheck.ports.repository import CheckRepository
from healthcheck.storage.memory_repo import InMemoryCheckRepository

VERDICT_IMAGE_DIR = Path(__file__).resolve().parent / "static" / "verdict"
from healthcheck.storage.sqlite_repo import SqliteCheckRepository

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
        article=FakeArticleExtractor(),
        link_resolver=FakeLinkResolver(),
    )


def _build_stance_classifier(settings: Settings):
    """논문 입장 분류기. rule이면 None(표현 목록 분류)을 돌려준다."""
    if settings.paper_stance_classifier != "liner":
        return None
    from healthcheck.adapters.liner.extraction_model import LinerExtractionModel
    from healthcheck.pipeline.paper_stance import LlmStanceClassifier

    # 평가셋 실측 주장 1건 최대 6.5초. 15초 안에 답이 없으면 그 묶음은 표현 목록 분류를 쓴다.
    model = LinerExtractionModel(
        settings.liner_api_key, reasoning_effort=settings.paper_stance_effort, timeout=15.0, max_retries=1
    )
    verifier = None
    if settings.paper_stance_verify_effort != "off":
        # 반박 후보만 다시 본다. 10초 안에 답이 없으면 첫 분류의 반박을 그대로 쓴다.
        verifier = LinerExtractionModel(
            settings.liner_api_key, reasoning_effort=settings.paper_stance_verify_effort, timeout=10.0, max_retries=1
        )
    logger.info(
        "논문 입장 분류: Liner Model API(%s), 반박 확인=%s", settings.paper_stance_effort, settings.paper_stance_verify_effort
    )
    return LlmStanceClassifier(model, verifier=verifier)


def _build_link_claim_extractor(settings: Settings, live: bool):
    """링크 본문용 주장 추출기. 키가 없거나 rule이면 None(규칙 기반)을 돌려준다."""
    choice = settings.link_claim_extractor
    if not live or choice == "rule":
        return None
    from healthcheck.pipeline.step3_llm import LlmClaimExtractor

    if choice == "liner" and settings.has_liner_key:
        from healthcheck.adapters.liner.extraction_model import LinerExtractionModel

        # 평가셋 실측 최대 17초. 30초 안에 답이 없으면 규칙 기반 추출로 대신한다(어르신 대기 시간 상한).
        model = LinerExtractionModel(
            settings.liner_api_key, reasoning_effort=settings.liner_reasoning_effort, timeout=30.0, max_retries=1
        )
        logger.info(
            "링크 주장 추출: Liner Model API(%s) × %d회 + 규칙 기반 합치기, 두 단계=%s",
            settings.liner_reasoning_effort,
            settings.link_claim_samples,
            settings.link_claim_two_stage,
        )
        return LlmClaimExtractor(
            model,
            merge_rule_claims=True,
            two_stage=settings.link_claim_two_stage,
            samples=settings.link_claim_samples,
        )
    if choice == "gemini" and settings.gemini_api_key:
        from healthcheck.adapters.gemini.extraction_model import GeminiExtractionModel

        logger.info("링크 주장 추출: Gemini(%s) + 규칙 기반 합치기", settings.gemini_model)
        return LlmClaimExtractor(
            GeminiExtractionModel(settings.gemini_api_key, model=settings.gemini_model),
            merge_rule_claims=True,
            two_stage=settings.link_claim_two_stage,
        )
    logger.warning("LINK_CLAIM_EXTRACTOR=%s이지만 키가 없어 규칙 기반을 씁니다", choice)
    return None


def build_orchestrator(settings: Settings | None = None) -> Orchestrator:
    """설정에 따라 실제 어댑터와 Fake를 골라 꽂는다.

    어댑터를 하나씩 독립적으로 바꾼다. Liner 키는 있고 식약처 키는 아직
    없는 지금 상황에서, 학술 검증만 실제로 돌리고 식약처는 Fake로 두는
    조합이 가능해야 한다.
    """
    settings = settings or load_settings()
    live = settings.adapter_mode == "live"

    if live and settings.has_liner_key:
        academic = LinerAcademicSearchClient(
            settings.liner_api_key, stance_classifier=_build_stance_classifier(settings)
        )
        logger.info("학술 검증: Liner 실제 호출")
    else:
        academic = FakeAcademicSearchClient()
        if live:
            logger.warning("ADAPTER_MODE=live이지만 LINER_API_KEY가 없어 Fake를 씁니다")

    if live and settings.has_mfds_key:
        mfds = FoodSafetyKoreaDataSource(
            settings.mfds_api_key, snapshot_path=settings.mfds_snapshot_path
        )
        logger.info("식약처: 식품안전나라 개별인정형 사본 우선, 없으면 오픈API")
    else:
        mfds = FakeMfdsDataSource()
        if live:
            logger.warning("ADAPTER_MODE=live이지만 MFDS_API_KEY가 없어 Fake를 씁니다")

    # 자막 추출은 키가 필요 없으므로 live 모드면 바로 실제 구현을 쓴다.
    if live:
        transcript = YouTubeTranscriptExtractor.from_settings(settings)
        logger.info("자막 추출: 유튜브 실제 호출 (한국어 자막만)")
        # 자막이 없으면 Gemini로 소리와 화면 글자를 옮긴다. 서버가 영상을 내려받지
        # 않으므로 유튜브 IP 차단과 무관하고, Whisper보다 빠르고 정확했다.
        if settings.gemini_video_enabled and settings.gemini_api_key:
            transcript = CaptionsThenAsrExtractor(
                transcript,
                GeminiVideoTranscriber(settings.gemini_api_key, model=settings.gemini_model),
            )
            logger.info("자막 없는 영상: Gemini 영상 이해 (%s)", settings.gemini_model)
        if settings.asr_enabled:
            # 자막이 없을 때만 음성 인식으로 넘어간다. 자막이 1초면 끝나는
            # 일을 분 단위 작업으로 대신할 이유가 없다.
            transcript = CaptionsThenAsrExtractor(
                transcript,
                WhisperAsrTranscriber(
                    model_size=settings.asr_model_size,
                    max_seconds=settings.asr_max_seconds,
                    proxy_url=settings.youtube_proxy_url,
                ),
            )
            logger.info(
                "음성 인식 사용: 모델 %s, 최대 %d초. 응답이 분 단위로 늦어질 수 있다.",
                settings.asr_model_size,
                settings.asr_max_seconds,
            )
        # 가장 바깥에 둔다. 자막과 음성 인식을 먼저 시도하고, 둘 다 없을 때만
        # 영상 제목으로 본문을 만든다. 안쪽에 두면 제목이 음성 인식을 가린다.
        transcript = TitledTranscriptExtractor(transcript, YouTubeTitleFetcher())
        logger.info("영상 제목: 자막 앞에 붙이고, 자막이 없으면 제목으로 판정")
    else:
        transcript = FakeTranscriptExtractor()

    if live and settings.article_enabled:
        article = TrafilaturaArticleExtractor()
        logger.info("기사 본문 추출: 사용")
    elif settings.article_enabled:
        article = FakeArticleExtractor()
    else:
        article = None

    link_claim_extractor = _build_link_claim_extractor(settings, live)

    return Orchestrator(
        transcript=transcript,
        academic=academic,
        mfds=mfds,
        link_claim_extractor=link_claim_extractor,
        claim_extractor=RuleBasedClaimExtractor(),
        ruleset=load_ruleset(settings.rules_path),
        verdict_cache=InMemoryVerdictCache(),
        extraction_cache=InMemoryExtractionCache(),
        article=article,
        link_resolver=HttpLinkResolver() if live else FakeLinkResolver(),
    )


def build_repository(settings: Settings | None = None) -> CheckRepository:
    """설정에 따라 저장소를 고른다.

    판정 기록은 재시작해도 남아야 한다. 누적 확인 건수와 오정보 유형 수가
    사업 목표 지표이기 때문이다.
    """
    settings = settings or load_settings()
    if settings.database_path in ("", ":memory:"):
        logger.warning("저장소가 메모리다. 재시작하면 판정 기록이 사라진다.")
        return InMemoryCheckRepository()
    logger.info("저장소: SQLite %s", settings.database_path)
    return SqliteCheckRepository(settings.database_path)


def build_callback_sender(settings: Settings | None = None) -> object:
    """실제 카카오 콜백 전송기를 쓸지 기록용 Fake를 쓸지 고른다."""
    settings = settings or load_settings()
    if settings.adapter_mode == "live":
        logger.info("콜백: 카카오로 실제 전송")
        return HttpCallbackSender(allowed_suffixes=settings.callback_allowed_suffixes)
    return RecordingCallbackSender()


def create_app(
    orchestrator: Orchestrator | None = None,
    callback_sender=None,
    *,
    worker_count: int = 2,
    repository: CheckRepository | None = None,
    auth: SkillAuth | None = None,
    skill_path: str | None = None,
    image_base_url: str | None = None,
) -> FastAPI:
    settings = load_settings()
    image_base_url = image_base_url or settings.public_base_url
    setup_logging(settings.log_level)
    auth = (
        auth
        if auth is not None
        else SkillAuth(
            header_name=settings.skill_auth_header, token=settings.skill_auth_token
        )
    )
    skill_path = skill_path or settings.skill_path

    orchestrator = orchestrator or build_orchestrator()
    callback_sender = callback_sender or RecordingCallbackSender()
    # 기본값을 메모리로 둔다. 테스트가 실수로 실제 DB 파일을 만들지 않게 한다.
    # 실제 서비스는 아래 app 조립에서 SQLite를 넘긴다.
    repository = repository or InMemoryCheckRepository()
    queue = InProcessJobQueue()
    last_results = skill.LastResults()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = await start_workers(
            queue,
            orchestrator,
            callback_sender,
            count=worker_count,
            repository=repository,
            last_results=last_results,
            image_base_url=image_base_url,
        )
        logger.info("워커 %d개 기동. 규칙표 %s", worker_count, orchestrator.ruleset.version)

        # 식약처 사본을 서버가 켜져 있는 동안 확인한다. 제한 없는 시간에만 받는다.
        refresh = getattr(orchestrator.mfds, "run_snapshot_refresh", None)
        if refresh is not None:
            tasks.append(asyncio.create_task(refresh()))
        if image_base_url:
            logger.info("결과 카드 이미지: %s", verdict_image_url(image_base_url, Label.TRUSTWORTHY))
        else:
            logger.info("PUBLIC_BASE_URL이 없어 이미지 없는 카드로 답한다")

        # 기동할 때 한 번 보존 기간을 정리한다. 배포 주기가 긴 환경에서는
        # scripts/purge_expired.py를 하루 한 번 돌리는 편이 낫다.
        async def purge_forever() -> None:
            # 서버를 오래 켜 두면 기동 때 한 번으로는 부족하다. 하루에 한 번 정리한다.
            while True:
                try:
                    purged = await repository.purge_expired()
                    if any(purged.values()):
                        logger.info("보존 기간 만료 정리: %s", purged)
                except Exception:
                    logger.exception("보존 기간 정리 실패")
                await asyncio.sleep(24 * 3600)

        tasks.append(asyncio.create_task(purge_forever()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()

    app = FastAPI(title="건강정보 확인", lifespan=lifespan)
    app.state.services = skill.SkillServices(
        orchestrator=orchestrator,
        queue=queue,
        repository=repository,
        auth=auth,
        last_results=last_results,
        image_base_url=image_base_url,
    )
    app.state.image_base_url = image_base_url
    app.state.callback_probe_enabled = settings.callback_probe_enabled
    if settings.callback_probe_enabled:
        logger.warning("콜백 유효 시간 시험이 켜져 있다. 시험이 끝나면 CALLBACK_PROBE_ENABLED를 끈다.")
    app.state.last_results = last_results
    app.state.auth = auth
    app.state.log_skill_request = settings.log_skill_request
    if settings.log_skill_request:
        logger.warning(
            "카카오 요청 본문 기록이 켜져 있다. 연동 확인이 끝나면 "
            "LOG_SKILL_REQUEST=0으로 꺼라. 발화 내용이 로그에 남는다."
        )
    app.state.repository = repository
    app.state.queue = queue
    app.state.callback_sender = callback_sender
    app.include_router(skill.build_router(skill_path))

    # 결과 카드 머리 이미지. 카카오가 내려받으므로 인증 없이 연다.
    # 정해진 파일 네 개만 내보낸다. 경로 조작으로 다른 파일을 읽지 못하게 한다.
    @app.get("/static/verdict/{version}/{name}.png", include_in_schema=False)
    async def verdict_image(version: str, name: str) -> FileResponse:
        if version != IMAGE_VERSION or name not in IMAGE_FILES.values():
            raise HTTPException(status_code=404)
        return FileResponse(
            VERDICT_IMAGE_DIR / f"{name}.png",
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=604800"},
        )
    warn_if_unprotected(auth, skill_path, settings.debug_ui)

    # 팀원 분류 시트. 토큰이 있을 때만 연다. 서비스 판정과 무관한 내부 도구다.
    if settings.labeling_token:
        from healthcheck.api import labeling

        app.include_router(
            labeling.build_router(settings.labeling_token, labeling.LabelingStore(settings.labeling_db_path))
        )
        logger.info("분류 시트: /labeling/<토큰>/ (저장 %s)", settings.labeling_db_path)

    # 디버그 채팅 화면. 개발용이며 DEBUG_UI=0으로 끈다. live와 fake 두 조립을
    # 함께 올려 두고 화면에서 고를 수 있게 한다. 규칙만 시험할 때 토큰을 아낀다.
    if settings.debug_ui:
        app.state.debug_orchestrators = {
            "live": orchestrator,
            "fake": build_fake_orchestrator(),
        }
        app.include_router(debug.router)
        logger.info("디버그 화면: http://127.0.0.1:8000/debug")

    @app.get("/metrics")
    async def metrics(request: Request) -> dict:
        """사업 목표 지표. 개인을 식별하는 값은 담지 않는다.

        집계값뿐이지만 서비스 이용량이 드러나므로 스킬 토큰과 같은 보호를 둔다.
        """
        auth.verify(request)
        values = await repository.metrics()
        return {
            "누적_확인_건수": values.total_checks,
            "라벨별": values.by_label,
            "오정보_유형_수": values.misinformation_types,
            "캐시_적중": values.cache_hits,
            "참여자_수": values.distinct_users,
            "규칙표_버전별": values.rules_versions,
        }

    @app.get("/health")
    async def health() -> dict:
        # 자막 추출이 차단 대기 중인지 노출한다. 배포 후 가장 먼저 깨지는
        # 지점이므로 외부에서 확인할 수 있어야 한다.
        blocked = getattr(orchestrator.transcript, "is_blocked", False)
        return {
            "status": "degraded" if blocked else "ok",
            "rules_version": orchestrator.ruleset.version,
            "queued": queue.size,
            "transcript_blocked": blocked,
            "gemini": _gemini_usage(orchestrator.transcript),
            "mfds_snapshot": (
                orchestrator.mfds.snapshot_info()
                if hasattr(orchestrator.mfds, "snapshot_info")
                else None
            ),
        }

    return app


def _gemini_usage(extractor, depth: int = 0) -> dict | None:
    """추출기 조립 안쪽의 Gemini 전사기를 찾아 오늘 사용량을 돌려준다."""
    if extractor is None or depth > 4:
        return None
    usage = getattr(extractor, "usage", None)
    if callable(usage):
        return usage()
    for name in ("_inner", "_captions", "_asr"):
        found = _gemini_usage(getattr(extractor, name, None), depth + 1)
        if found is not None:
            return found
    return None


_settings = load_settings()
app = create_app(
    repository=build_repository(_settings),
    callback_sender=build_callback_sender(_settings),
)
