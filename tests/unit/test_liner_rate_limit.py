"""④ 학술 검색 점검에서 찾은 결함 테스트.

실측에서 동시 4건 중 2건이 429로 거절됐다(retry-after: 1). 1초 간격 순차 호출은
모두 통과했다. 3단계에서 주장 3건을 동시에 검증하게 바꿔 운영 중에 학술 근거가
자주 빠질 상황이었다.
"""

from __future__ import annotations

import asyncio

import httpx

from healthcheck.adapters.liner.academic import LinerAcademicSearchClient, is_relevant
from healthcheck.domain.types import Claim

GINSENG = Claim(ingredient="홍삼", claimed_effect="면역력 증진에 도움을", intensity="SUGGESTIVE", span=(0, 2))
OK_BODY = {
    "results": [
        {"title": "Korean red ginseng enhances immune function", "url": "https://e.org/1", "journal": "J Ginseng Res"}
    ]
}


def client_with(handler, **kwargs) -> LinerAcademicSearchClient:
    return LinerAcademicSearchClient(
        "test-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        rate_limit_wait=0.01,
        **kwargs,
    )


# ── 호출 제한 ────────────────────────────────────────────────


async def test_429를_받으면_Retry_After만큼_기다렸다가_다시_보낸다():
    calls: list[float] = []

    def handler(request):
        calls.append(asyncio.get_event_loop().time())
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0.05"}, json={"error": {"retryable": True}})
        return httpx.Response(200, json=OK_BODY)

    papers = await client_with(handler).search(GINSENG)

    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.05
    assert papers


async def test_429가_이어져도_여러_번_재시도한다():
    """이전에는 한 번만 재시도했다. 동시 요청이 몰리면 부족했다."""
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) <= 3:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json=OK_BODY)

    assert await client_with(handler).search(GINSENG)
    assert len(calls) == 4


async def test_요청_시작_간격을_초당_요청_수에_맞춘다():
    """Liner 공식 제한은 초당 2건이다. 동시 호출 수가 아니라 시작 간격을 막는다.

    응답이 빠르면 동시 2건 제한으로도 초당 6건이 나가 429를 받는다.
    """
    starts: list[float] = []

    def handler(request):
        starts.append(asyncio.get_event_loop().time())
        return httpx.Response(200, json={"results": []})

    client = client_with(handler, max_qps=20, cache_ttl=0)  # 간격 0.05초
    claims = [
        Claim(ingredient="홍삼", claimed_effect=f"면역력 {i}", intensity="SUGGESTIVE", span=(0, 1))
        for i in range(4)
    ]
    await asyncio.gather(*(client.search(claim, max_results=i + 1) for i, claim in enumerate(claims)))

    starts.sort()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(starts) == 4
    assert min(gaps) >= 0.045


async def test_재시도_불가로_표시된_429는_다시_보내지_않는다():
    """공식 권장: retryable이 참일 때만 재시도한다."""
    import pytest

    from healthcheck.adapters.liner.academic import AcademicSearchError

    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json={"error": {"code": "RATE_LIMITED", "retryable": False}})

    with pytest.raises(AcademicSearchError):
        await client_with(handler).search(GINSENG)
    assert len(calls) == 1


# ── 캐시 ─────────────────────────────────────────────────────


async def test_같은_질의는_캐시로_답한다():
    """같은 성분·효능 주장은 다른 영상과 글에서도 반복된다."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=OK_BODY)

    client = client_with(handler)
    await client.search(GINSENG)
    await client.search(GINSENG)

    assert len(calls) == 1
    assert client.cache_hits == 1


async def test_실패한_결과는_캐시하지_않는다():
    """조회에 실패한 질의는 다음 요청에서 다시 조회해야 한다."""
    import pytest

    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=OK_BODY)

    client = client_with(handler, max_retries=0)
    with pytest.raises(Exception):
        await client.search(GINSENG)

    assert await client.search(GINSENG)
    assert len(calls) == 2
    assert client.cache_hits == 0


# ── 관련성과 파일형 결과 ─────────────────────────────────────


def test_효능은_같아도_다른_성분_논문은_근거가_아니다():
    assert is_relevant("Beetroot juice lowers blood pressure", None, "garlic", "blood pressure") is False
    assert is_relevant("Garlic supplementation lowers blood pressure", None, "garlic", "blood pressure") is True


def test_영문_표기가_없는_성분은_성분_검사를_건너뛴다():
    assert is_relevant("A study on cough relief", None, "산수유", "cough") is True


def test_파일_목록처럼_보이는_결과는_근거가_아니다():
    """실측에서 "vitamin c immunity pdf"가 뒷받침 논문으로 세어졌다."""
    assert is_relevant("vitamin c immunity pdf", None, "vitamin C", "immune function") is False
    assert is_relevant("Vitamin C and Immune Function", None, "vitamin C", "immune function") is True
