"""파이프라인 전체 실행과 캐시 분기.

① 입력 → ② 추출 → ③ 구조화 → ④ 검증 → ⑤ 판정 순서로 돌린다.
⑥ 응답 생성은 호출자(워커 또는 스킬 엔드포인트)가 맡는다. 판정 결과와
표현 형식을 분리해 두면 같은 결과를 동기 응답과 콜백 양쪽에 쓸 수 있다.

모든 중간 산출물을 로그로 남긴다. 누적 확인 건수와 오정보 유형 수가
사업 목표 지표이므로, 판정 로그에서 바로 집계할 수 있어야 한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from healthcheck.domain.types import (
    CheckResult,
    ClaimSet,
    ExtractedContent,
    ShareInput,
)
from healthcheck.pipeline import step1_input
from healthcheck.pipeline.step3_claims import ClaimExtractor
from healthcheck.pipeline.step4_verify import verify
from healthcheck.pipeline.step5_verdict import RuleSet, build_verdict_input
from healthcheck.ports.academic import AcademicSearchClient
from healthcheck.ports.cache import ExtractionCache, VerdictCache
from healthcheck.ports.mfds import MfdsDataSource
from healthcheck.ports.transcript import TranscriptExtractor

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Orchestrator:
    transcript: TranscriptExtractor
    academic: AcademicSearchClient
    mfds: MfdsDataSource
    claim_extractor: ClaimExtractor
    ruleset: RuleSet
    verdict_cache: VerdictCache
    extraction_cache: ExtractionCache

    async def cached(self, share_input: ShareInput) -> CheckResult | None:
        """캐시 1단만 조회한다. 5초 동기 응답 경로에서 쓴다.

        여기서 외부 호출을 하지 않는다. 자막을 가져오는 순간 5초를 보장할 수
        없기 때문이다. 그래서 동기 경로는 이 조회 하나로 끝나야 한다.
        """
        key = step1_input.cache_key(share_input)
        found = await self.verdict_cache.get(key, rules_version=self.ruleset.version)
        if found is None:
            return None
        logger.info("캐시 적중: key=%s version=%s", key, self.ruleset.version)
        return _mark_cached(found)

    async def run(self, share_input: ShareInput) -> CheckResult:
        """전체 파이프라인. 워커가 호출한다."""
        cached = await self.cached(share_input)
        if cached is not None:
            return cached

        content = await self._content_of(share_input)
        claims = await self._claims_of(content)

        academic = None
        mfds = None

        # 범위 밖이면 외부 조회를 하지 않는다. 판정은 R-001에서 끝나므로
        # 조회해도 쓰이지 않고 Liner 토큰만 쓴다.
        if claims.in_scope and claims.claims:
            primary = claims.claims[0]
            academic, mfds = await verify(primary, academic=self.academic, mfds=self.mfds)

        verdict_input = build_verdict_input(
            claims=claims,
            content=content,
            academic=academic,
            mfds=mfds,
            thresholds=self.ruleset.thresholds,
        )
        verdict = self.ruleset.evaluate(verdict_input)

        result = CheckResult(
            verdict=verdict,
            claims=claims,
            academic=academic,
            mfds=mfds,
            content=content,
            from_cache=False,
            source_urls=(share_input.url,) if share_input.url else (),
        )

        _log_result(share_input, result)
        await self.verdict_cache.set(
            step1_input.cache_key(share_input), result, rules_version=self.ruleset.version
        )
        return result

    async def _content_of(self, share_input: ShareInput) -> ExtractedContent:
        """② 내용 추출."""
        if share_input.kind == "YOUTUBE" and share_input.video_id:
            return await self.transcript.fetch(share_input.video_id)

        if share_input.kind == "PLAIN_TEXT" and share_input.raw_text.strip():
            # 어르신이 링크 없이 주장을 직접 적어 보낸 경우다. 자막 추출이 필요 없다.
            import hashlib

            text = share_input.raw_text
            return ExtractedContent(
                text=text,
                has_transcript=True,
                confidence=1.0,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:32],
            )

        # 유튜브가 아닌 링크. 이번 MVP는 내용을 추출하지 않는다.
        return ExtractedContent(text="", has_transcript=False, confidence=0.0)

    async def _claims_of(self, content: ExtractedContent) -> ClaimSet:
        """③ 주장 구조화. 캐시 2단을 쓴다."""
        if not content.has_transcript or not content.text.strip():
            return ClaimSet(claims=(), in_scope=True, disease_cure_expression=False, extraction_failed=True)

        if content.content_hash:
            found = await self.extraction_cache.get(content.content_hash)
            if found is not None:
                return found

        claims = self.claim_extractor.extract(content.text)

        if content.content_hash:
            await self.extraction_cache.set(content.content_hash, claims)

        return claims


def _mark_cached(result: CheckResult) -> CheckResult:
    from dataclasses import replace

    return replace(result, from_cache=True)


def _log_result(share_input: ShareInput, result: CheckResult) -> None:
    """판정 로그. 목표 지표 집계의 원천이다.

    남기는 값: 입력 종류, 라벨, 규칙 ID, 규칙 버전, 성분, 표현 강도.
    오정보 유형 수는 (라벨, 규칙 ID, 성분) 조합으로 집계한다.
    원문과 카카오 사용자 키는 여기에 남기지 않는다. 개인정보 보존 정책은
    저장소 계층에서 따로 관리한다.
    """
    verdict = result.verdict
    claims = result.claims
    primary = claims.claims[0] if claims and claims.claims else None

    logger.info(
        "판정 완료 kind=%s label=%s rule=%s version=%s 성분=%s 강도=%s 범위내=%s 자막=%s",
        share_input.kind,
        verdict.label,
        verdict.rule_id,
        verdict.rules_version,
        primary.ingredient if primary else "-",
        primary.intensity if primary else "-",
        claims.in_scope if claims else "-",
        verdict.inputs.transcript_quality if verdict.inputs else "-",
    )
