"""파이프라인 전체 실행과 캐시 분기.

① 입력 → ② 추출 → ③ 구조화 → ④ 검증 → ⑤ 판정 순서로 돌린다.
⑥ 응답 생성은 호출자(워커 또는 스킬 엔드포인트)가 맡는다. 판정 결과와
표현 형식을 분리해 두면 같은 결과를 동기 응답과 콜백 양쪽에 쓸 수 있다.

모든 중간 산출물을 로그로 남긴다. 누적 확인 건수와 오정보 유형 수가
사업 목표 지표이므로, 판정 로그에서 바로 집계할 수 있어야 한다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from healthcheck.domain.labels import Label
from healthcheck.domain.types import (
    CheckResult,
    ClaimCheck,
    ClaimSet,
    ExtractedContent,
    ShareInput,
)
from healthcheck.adapters.youtube.transcript import TranscriptFetchError
from healthcheck.pipeline import step1_input
from healthcheck.pipeline.step3_claims import ClaimExtractor
from healthcheck.pipeline.step4_verify import verify
from healthcheck.pipeline.step5_verdict import (
    RuleSet,
    _transcript_quality,
    build_verdict_input,
)
from healthcheck.ports.academic import AcademicSearchClient
from healthcheck.ports.article import ArticleExtractor
from healthcheck.ports.links import LinkResolver
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
    # 유튜브가 아닌 링크의 본문을 뽑는다. 없으면 그런 링크는 판정 보류가 된다.
    article: ArticleExtractor | None = None
    # 단축 링크를 푼다. 없으면 단축 링크는 풀지 않은 채 기사로 처리된다.
    link_resolver: LinkResolver | None = None

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

    async def run(
        self, share_input: ShareInput, *, trace: dict[str, float] | None = None
    ) -> CheckResult:
        """전체 파이프라인. 워커가 호출한다.

        trace에 dict를 넘기면 단계별 소요 시간(밀리초)을 채워 준다.
        디버그 화면이 어느 단계가 느린지 보기 위해 쓴다. 판정에는 영향이 없다.
        """
        started = time.monotonic()

        cached = await self.cached(share_input)
        if cached is not None:
            _record(trace, "cache", started)
            return cached

        # 단축 링크는 풀어야 실제 대상을 안다. 원래 키를 기억해 두었다가 결과를
        # 두 키 모두에 저장한다. 같은 단축 링크가 다시 오면 동기 경로에서
        # 바로 맞히고, 긴 주소로 먼저 확인된 글이면 여기서 맞힌다.
        original_key = step1_input.cache_key(share_input)
        if share_input.needs_resolution:
            share_input = await self._resolve(share_input, trace)
            cached = await self.cached(share_input)
            if cached is not None:
                await self.verdict_cache.set(
                    original_key, cached, rules_version=self.ruleset.version
                )
                _record(trace, "cache", started)
                return cached

        if share_input.kind == "YOUTUBE_OTHER":
            # 채널·재생목록 링크다. 확인할 영상이 없으므로 조회하지 않고 안내한다.
            return self._notice_result(share_input, "youtube_not_video")

        if share_input.kind == "OTHER_LINK":
            # 로그인해야 볼 수 있는 곳이다. 가져가도 로그인 화면만 받는다.
            # 막을 수 없는 한계이므로 '알 수 없어요' 대신 이유와 다른 방법을 알린다.
            site = step1_input.login_wall_site(share_input.url)
            if site:
                return self._notice_result(share_input, f"login_required:{site}")

        step = time.monotonic()
        content, retryable_failure = await self._content_of(share_input)
        content = _cap_content(content)
        _record(trace, "content", step)

        step = time.monotonic()
        claims = await self._claims_of(content)
        _record(trace, "claims", step)

        claim_checks = await self._check_claims(claims, content, trace)

        step = time.monotonic()
        if claim_checks:
            # 가장 엄한 결과를 쓴다. 뒤에 나온 주장이 더 위험해도 반영되게 한다.
            decisive = max(claim_checks, key=lambda check: SEVERITY[check.verdict.label])
            verdict = decisive.verdict
            academic, mfds, primary_claim = decisive.academic, decisive.mfds, decisive.claim
        else:
            verdict_input = build_verdict_input(
                claims=claims,
                content=content,
                academic=None,
                mfds=None,
                thresholds=self.ruleset.thresholds,
            )
            verdict = self.ruleset.evaluate(verdict_input)
            academic = mfds = primary_claim = None
        _record(trace, "verdict", step)

        result = CheckResult(
            verdict=verdict,
            claims=claims,
            academic=academic,
            mfds=mfds,
            content=content,
            primary_claim=primary_claim,
            claim_checks=tuple(claim_checks),
            from_cache=False,
            retryable_failure=retryable_failure,
            source_urls=(share_input.url,) if share_input.url else (),
        )

        _log_result(share_input, result)

        # 일시적 실패로 나온 판정은 캐시에 넣지 않는다. 자막 서버 차단 때문에
        # 나온 '알 수 없어요'를 캐시하면, 차단이 풀린 뒤에도 그 영상은 계속
        # 같은 답을 받는다. 캐시가 오답을 굳히는 셈이다.
        # 식약처나 Liner 조회가 실패한 채 나온 판정은 캐시하지 않는다. 근거가 빠진
        # 판정이 굳으면, 조회가 되살아난 뒤에도 같은 영상에 같은 답을 준다.
        # 2단계에서 기사 추출 실패에는 막았는데 검증 단계에는 빠져 있었다.
        evidence_failed = any(
            (check.academic is not None and check.academic.lookup_failed)
            or (check.mfds is not None and check.mfds.lookup_failed)
            for check in claim_checks
        )
        if evidence_failed:
            logger.warning("검증 조회 일부가 실패해 이 판정은 캐시하지 않는다")

        if not retryable_failure and not evidence_failed:
            keys = {original_key, step1_input.cache_key(share_input)}
            for key in keys:
                await self.verdict_cache.set(key, result, rules_version=self.ruleset.version)
        _record(trace, "total", started)
        return result

    async def _check_claims(
        self,
        claims: ClaimSet,
        content: ExtractedContent,
        trace: dict[str, float] | None,
    ) -> list[ClaimCheck]:
        """주장을 최대 MAX_VERIFIED_CLAIMS건까지 검증한다.

        이전에는 본문에 먼저 나온 주장 하나만 검증했다. 영상 자막 한 편에서 주장
        10건이 나왔는데 "홍삼 / 눈" 한 건만 확인했다. 뒤에 위험한 주장이 있어도
        판정에 반영되지 않았다.

        단정 표현을 먼저 고른다. 같은 강도면 본문 순서를 따른다. 검증은 동시에
        보낸다. 상한을 둔 이유는 Liner 토큰 비용이다.
        """
        if not self._needs_verification(claims, content):
            return []

        order = {"ASSERTIVE": 0, "SUGGESTIVE": 1, "UNCERTAIN": 2}
        seen: set[tuple[str, str]] = set()
        candidates = []
        for claim in sorted(claims.claims, key=lambda c: order.get(c.intensity, 3)):
            key = (claim.ingredient, claim.claimed_effect.replace(" ", ""))
            if key not in seen:
                seen.add(key)
                candidates.append(claim)
        candidates = candidates[:MAX_VERIFIED_CLAIMS]

        step = time.monotonic()
        evidence = await asyncio.gather(
            *(verify(claim, academic=self.academic, mfds=self.mfds) for claim in candidates)
        )
        _record(trace, "verify", step)

        checks = []
        for claim, (academic, mfds) in zip(candidates, evidence):
            verdict_input = build_verdict_input(
                claims=claims,
                content=content,
                academic=academic,
                mfds=mfds,
                thresholds=self.ruleset.thresholds,
            )
            checks.append(
                ClaimCheck(claim=claim, verdict=self.ruleset.evaluate(verdict_input), academic=academic, mfds=mfds)
            )
        return checks

    async def _resolve(
        self, share_input: ShareInput, trace: dict[str, float] | None
    ) -> ShareInput:
        """단축 링크를 풀고 종류를 다시 판별한다.

        풀지 못하면 원래 입력을 그대로 둔다. 기사 추출이 실패해 판정이 보류될
        뿐 오류로 끝나지는 않는다.
        """
        if self.link_resolver is None or not share_input.url:
            return share_input

        step = time.monotonic()
        try:
            final_url = await self.link_resolver.resolve(share_input.url)
        except Exception as error:
            logger.warning("단축 링크 해석 실패 url=%s: %s", share_input.url, error)
            _record(trace, "resolve", step)
            return share_input
        _record(trace, "resolve", step)

        resolved = step1_input.classify(final_url)
        return ShareInput(
            raw_text=share_input.raw_text,
            kind=resolved.kind,
            video_id=resolved.video_id,
            url=resolved.url or final_url,
            truncated=share_input.truncated,
            needs_resolution=False,
        )

    def _notice_result(self, share_input: ShareInput, notice: str) -> CheckResult:
        """판정 없이 안내만 보내는 결과. 규칙표를 거치지 않았음을 규칙 ID로 남긴다."""
        from healthcheck.domain.labels import Label
        from healthcheck.domain.types import Verdict

        logger.info("안내로 응답 kind=%s notice=%s", share_input.kind, notice)
        return CheckResult(
            verdict=Verdict(
                label=Label.UNKNOWN,
                rule_id=f"INPUT-{notice.split(':', 1)[0].upper()}",
                rules_version=self.ruleset.version,
                reasons=(),
            ),
            notice=notice,
            source_urls=(share_input.url,) if share_input.url else (),
        )

    def _needs_verification(self, claims: ClaimSet, content: ExtractedContent) -> bool:
        """④ 이중 검증을 돌릴 필요가 있는가.

        판정이 이미 정해진 입력에는 외부 조회를 하지 않는다. 조회해도 쓰이지
        않고 Liner 토큰과 응답 시간만 쓴다. 산학협력으로 토큰 비용을
        지원받는 구조이므로 낭비를 줄이는 것이 설계 목표에 들어간다.

        건너뛰는 경우 셋.
          · 주장을 하나도 뽑지 못했다 (조회할 성분이 없다)
          · 범위 밖이다 (R-001이 거절한다)
          · 자막이 없거나 신뢰도가 낮다 (R-010이 판정을 보류한다)
        """
        if not claims.claims or not claims.in_scope:
            return False

        quality = _transcript_quality(content, self.ruleset.thresholds)
        return quality == "ok"

    async def _content_of(self, share_input: ShareInput) -> tuple[ExtractedContent, bool]:
        """② 내용 추출.

        (내용, 일시적_실패) 쌍을 돌려준다. 일시적 실패면 판정을 캐시하지 않는다.
        """
        if share_input.kind == "YOUTUBE" and share_input.video_id:
            try:
                return await self.transcript.fetch(share_input.video_id), False
            except TranscriptFetchError as error:
                # 자막 서버 차단이나 일시 장애다. 자막이 없는 것과 결과는 같지만
                # 다시 시도하면 달라질 수 있으므로 캐시하지 않는다.
                logger.warning(
                    "자막 서버 접근 실패 video=%s: %s",
                    share_input.video_id,
                    str(error).splitlines()[0],
                )
                return _no_content(), True
            except Exception:
                logger.exception("자막 추출 실패 video=%s", share_input.video_id)
                return _no_content(), False

        if share_input.kind == "PLAIN_TEXT" and share_input.raw_text.strip():
            # 어르신이 링크 없이 주장을 직접 적어 보낸 경우다. 자막 추출이 필요 없다.
            import hashlib

            text = share_input.raw_text
            return (
                ExtractedContent(
                    text=text,
                    has_transcript=True,
                    confidence=1.0,
                    content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:32],
                ),
                False,
            )

        if share_input.kind == "OTHER_LINK" and share_input.url and self.article is not None:
            try:
                return await self.article.fetch(share_input.url), False
            except Exception as error:
                # 일시적 실패(통신 오류, 5xx, 429)는 캐시하지 않는다. 사이트가 잠깐
                # 503을 돌려준 것만으로 '알 수 없어요'가 굳으면, 사이트가 살아난
                # 뒤에도 같은 답을 준다. 404나 차단처럼 다시 해도 같은 실패만 캐시한다.
                retryable = bool(getattr(error, "retryable", False))
                logger.warning(
                    "기사 본문 추출 실패 url=%s 재시도가능=%s: %s",
                    share_input.url,
                    retryable,
                    error,
                )
                return _no_content(), retryable

        return _no_content(), False

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


def _record(trace: dict[str, float] | None, name: str, started: float) -> None:
    if trace is not None:
        trace[name] = round((time.monotonic() - started) * 1000, 1)


# 주장 추출에 넘기는 본문의 상한. 긴 방송 영상 자막이 26만 자까지 나왔다.
# 규칙 기반 추출이 느려지고 DB에 그대로 저장된다. 건강정보의 핵심 주장은
# 대체로 앞부분에 반복되므로 앞에서부터 자른다. 기사는 추출 단계에서 이미
# 크기를 제한하지만 자막과 음성 인식은 제한이 없어서 여기서 공통으로 자른다.
MAX_CONTENT_CHARS = 20_000


def _cap_content(content: ExtractedContent) -> ExtractedContent:
    if len(content.text) <= MAX_CONTENT_CHARS:
        return content
    import hashlib
    from dataclasses import replace

    text = content.text[:MAX_CONTENT_CHARS]
    logger.info("본문이 길어 앞 %d자만 쓴다. 원래 %d자", MAX_CONTENT_CHARS, len(content.text))
    return replace(
        content,
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:32],
    )


# 한 요청에서 검증할 주장의 최대 수. 주장마다 Liner와 식약처를 호출한다.
MAX_VERIFIED_CLAIMS = 3

# 여러 주장의 판정 중 무엇을 쓸지 정하는 엄격함 순서.
# UNKNOWN을 TRUSTWORTHY보다 엄하게 둔다. 확인하지 못한 주장이 섞였는데
# 전체를 '믿을 만해요'라고 말하면 과장이다. 모든 주장이 믿을 만할 때만 믿을 만하다.
SEVERITY = {Label.TRUSTWORTHY: 0, Label.UNKNOWN: 1, Label.CAUTION: 2, Label.WARNING: 3}


def _no_content() -> ExtractedContent:
    return ExtractedContent(text="", has_transcript=False, confidence=0.0)


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
    primary = result.primary_claim or (claims.claims[0] if claims and claims.claims else None)

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
