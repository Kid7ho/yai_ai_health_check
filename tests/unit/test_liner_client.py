"""Liner 실제 클라이언트 단위 테스트.

네트워크를 타지 않는다. httpx MockTransport로 응답을 흉내 낸다.
실제 호출 확인은 별도로 했고, 여기서는 응답 해석과 실패 처리를 고정한다.
"""

from __future__ import annotations

import httpx
import pytest

from healthcheck.adapters.liner.academic import (
    SCHOLAR_URL,
    AcademicSearchError,
    LinerAcademicSearchClient,
    build_query,
    classify_stance,
    is_relevant,
)
from healthcheck.domain.types import Claim

GINSENG = Claim(
    ingredient="홍삼", claimed_effect="면역력 증진에 도움을", intensity="SUGGESTIVE", span=(0, 2)
)
BLUEBERRY = Claim(
    ingredient="블루베리", claimed_effect="당뇨를 완치시킨다", intensity="ASSERTIVE", span=(0, 4)
)

# 실제 응답 형식을 그대로 따른다.
SAMPLE_RESPONSE = {
    "requestId": "req_abc123",
    "results": [
        {
            "title": "Korean red ginseng enhances antiviral immunity",
            "url": "https://example.org/a",
            "hostname": "example.org",
            "description": "Immune markers improved significantly.",
            "date": "2025-06-01",
            "citationCount": 5,
            "authors": ["Kim", "Park"],
            "journal": "Journal of Ginseng Research",
        },
        {
            "title": "No evidence that red ginseng affects immune function",
            "url": "https://example.org/b",
            "description": None,
            "date": "2020-01-01",
            "citationCount": 12,
            "authors": [],
            "journal": "Nutrients",
        },
        {
            # 특허·제조법 문헌. 근거가 아니므로 걸러져야 한다.
            "title": "Method for preparing Korean red ginseng immune powder",
            "url": "https://example.org/c",
            "citationCount": 0,
        },
    ],
    "totalCount": 3,
}


def client_with(handler) -> LinerAcademicSearchClient:
    transport = httpx.MockTransport(handler)
    return LinerAcademicSearchClient(
        "test-key", client=httpx.AsyncClient(transport=transport), max_retries=1
    )


# ─────────────────────────────────────────────────────────────
# 검색어 만들기
# ─────────────────────────────────────────────────────────────


def test_한국어_성분과_효능을_영문_검색어로_바꾼다():
    """학술 논문은 대부분 영어다. 한국어로 검색하면 결과가 거의 없다."""
    assert build_query(GINSENG) == "Korean red ginseng immune function"
    assert build_query(BLUEBERRY) == "blueberry anthocyanin diabetes"


def test_영문_표기가_없는_성분은_한국어로_보낸다():
    claim = Claim(ingredient="도라지", claimed_effect="기침에 좋다", intensity="SUGGESTIVE", span=(0, 3))
    assert build_query(claim) == "도라지"


# ─────────────────────────────────────────────────────────────
# 찬반 판정과 관련성
# ─────────────────────────────────────────────────────────────


def test_반박_표현을_뒷받침보다_먼저_본다():
    """제목에 부정문이 섞인 논문을 뒷받침으로 세면 판정이 뒤집힌다."""
    assert classify_stance("No significant improvement in immune function", None) == "REFUTE"


def test_어느_쪽에도_걸리지_않으면_중립이다():
    """판단 불가를 뒷받침으로 세면 보수적 기본값 원칙이 무너진다."""
    assert classify_stance("Saponin content of Korean red ginseng", None) == "NEUTRAL"


def test_특허와_제조법_문헌은_근거가_아니다():
    """실제 호출에서 추출 방법 특허 세 건이 당뇨 주장의 뒷받침으로 세어졌다."""
    assert is_relevant("Method for extracting blueberry anthocyanin", None, "blueberry", "diabetes") is False


def test_효능이_언급되지_않은_문헌은_그_효능의_근거가_아니다():
    """Liner는 검색어에 효능을 넣어도 성분만 맞는 문헌을 함께 돌려준다."""
    assert is_relevant("Mineral components of Korean red ginseng", None, "Korean red ginseng", "immune function") is False
    assert is_relevant("Red ginseng and immunity in mice", None, "Korean red ginseng", "immune function") is True


# ─────────────────────────────────────────────────────────────
# 응답 해석
# ─────────────────────────────────────────────────────────────


async def test_응답을_Paper로_옮긴다():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(SCHOLAR_URL)
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(200, json=SAMPLE_RESPONSE)

    client = client_with(handler)
    papers = await client.search(GINSENG)

    # 특허 문헌이 걸러져 두 건만 남는다.
    assert len(papers) == 2
    assert papers[0].journal == "Journal of Ginseng Research"
    assert papers[0].citation_count == 5
    assert papers[0].authors == ("Kim", "Park")
    assert papers[0].stance == "SUPPORT"
    assert papers[1].stance == "REFUTE"


async def test_max_results는_1에서_20_사이로_맞춘다():
    """문서가 정한 범위를 넘기면 요청이 거부된다."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"results": []})

    client = client_with(handler)
    await client.search(GINSENG, max_results=50)
    assert captured["max_results"] == 20

    await client.search(GINSENG, max_results=0)
    assert captured["max_results"] == 1


async def test_결과가_없으면_빈_튜플이다():
    client = client_with(lambda request: httpx.Response(200, json={"results": []}))
    assert await client.search(GINSENG) == ()


# ─────────────────────────────────────────────────────────────
# 실패 처리
# ─────────────────────────────────────────────────────────────


async def test_키가_잘못되면_재시도하지_않고_바로_실패한다():
    """401은 재시도해도 같은 결과다. 응답 지연만 늘어난다."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, text="unauthorized")

    client = client_with(handler)
    with pytest.raises(AcademicSearchError, match="401"):
        await client.search(GINSENG)
    assert len(calls) == 1


async def test_호출_제한과_서버_오류는_재시도한다():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json=SAMPLE_RESPONSE)

    client = client_with(handler)
    papers = await client.search(GINSENG)
    assert len(calls) == 2
    assert papers


async def test_통신_오류가_계속되면_예외를_올린다():
    """빈 결과를 돌려주면 '논문이 없다'와 '조회 실패'가 구분되지 않는다."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결 실패")

    client = client_with(handler)
    with pytest.raises(AcademicSearchError):
        await client.search(GINSENG)


async def test_조회_실패는_판정_단계에서_UNKNOWN으로_흡수된다():
    """④ 검증 단계가 예외를 잡아 lookup_failed로 바꾼다."""
    from healthcheck.adapters.fake.mfds import FakeMfdsDataSource
    from healthcheck.pipeline.step4_verify import verify

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client = client_with(handler)
    academic, _ = await verify(GINSENG, academic=client, mfds=FakeMfdsDataSource())

    assert academic.lookup_failed is True
    assert academic.supporting == 0


def test_키가_없으면_클라이언트를_만들_수_없다():
    with pytest.raises(ValueError, match="LINER_API_KEY"):
        LinerAcademicSearchClient("")
