"""④ 이중 검증 — A) 학술 검색, B) 식약처 기록 조회.

두 조회는 서로 의존하지 않으므로 동시에 보낸다. 파이프라인 전체가 10~20초
걸리는 구간의 대부분이 여기이므로 순차로 돌리면 지연이 두 배가 된다.

한쪽 조회가 실패해도 예외를 밖으로 던지지 않는다. lookup_failed를 세워
돌려주고, 판정 단계가 그것을 UNKNOWN으로 흡수한다. 조회 실패 때문에
어르신에게 아무 답도 못 보내는 상황을 만들지 않는다.
"""

from __future__ import annotations

import asyncio
import logging

from healthcheck.domain.types import AcademicEvidence, Claim, MfdsEvidence, Paper
from healthcheck.ports.academic import AcademicSearchClient
from healthcheck.ports.mfds import MfdsDataSource

logger = logging.getLogger(__name__)


def summarize_papers(papers: tuple[Paper, ...]) -> AcademicEvidence:
    """논문 목록을 뒷받침·반박 수로 집계한다.

    NEUTRAL은 어느 쪽으로도 세지 않는다. 판단 불가를 뒷받침으로 세면
    보수적 기본값 원칙이 무너진다.
    """
    return AcademicEvidence(
        supporting=sum(1 for paper in papers if paper.stance == "SUPPORT"),
        refuting=sum(1 for paper in papers if paper.stance == "REFUTE"),
        papers=papers,
    )


async def verify(
    claim: Claim,
    *,
    academic: AcademicSearchClient,
    mfds: MfdsDataSource,
) -> tuple[AcademicEvidence, MfdsEvidence]:
    """학술 검색과 식약처 조회를 동시에 보낸다."""
    academic_task = asyncio.create_task(_search(claim, academic))
    mfds_task = asyncio.create_task(_lookup(claim, mfds))
    return await academic_task, await mfds_task


async def _search(claim: Claim, client: AcademicSearchClient) -> AcademicEvidence:
    try:
        papers = await client.search(claim)
    except Exception:
        logger.exception("학술 검색 실패: %s", claim.ingredient)
        return AcademicEvidence(supporting=0, refuting=0, lookup_failed=True)
    return summarize_papers(papers)


async def _lookup(claim: Claim, source: MfdsDataSource) -> MfdsEvidence:
    try:
        return await source.lookup(claim)
    except Exception:
        logger.exception("식약처 조회 실패: %s", claim.ingredient)
        return MfdsEvidence(
            recognition="UNKNOWN",
            functionality_text=None,
            match_level="NOT_APPLICABLE",
            lookup_failed=True,
        )
