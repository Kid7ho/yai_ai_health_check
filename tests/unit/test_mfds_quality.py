"""⑤ 공공데이터 검증 점검에서 찾은 결함 테스트."""

from __future__ import annotations

import httpx

from healthcheck.adapters.mfds.foodsafetykorea import FoodSafetyKoreaDataSource
from healthcheck.domain.types import Claim


def claim(ingredient: str, effect: str) -> Claim:
    return Claim(ingredient=ingredient, claimed_effect=effect, intensity="SUGGESTIVE", span=(0, 1))


def payload(rows):
    return {"I-0040": {"total_count": str(len(rows)), "row": rows, "RESULT": {"CODE": "INFO-000"}}}


def source_with(handler) -> FoodSafetyKoreaDataSource:
    return FoodSafetyKoreaDataSource("test-key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def row(name: str, functionality: str, number: str = "2020-1") -> dict:
    return {"APLC_RAWMTRL_NM": name, "FNCLTY_CN": functionality, "HF_FNCLTY_MTRAL_RCOGN_NO": number}


# ── 영양성분 ─────────────────────────────────────────────────


async def test_칼슘의_뼈_건강_주장은_고시형_인정과_일치한다():
    """실측에서 영양성분 8종이 모두 '미인정'으로 처리됐다."""

    def handler(request):  # pragma: no cover
        raise AssertionError("고시형 영양성분에 API를 호출했다")

    evidence = await source_with(handler).lookup(claim("칼슘", "뼈 건강에 도움을"))

    assert evidence.recognition == "NOTIFIED"
    assert evidence.match_level == "EXACT"


async def test_비타민C의_면역력_주장은_인정_범위_밖이다():
    """비타민C의 고시형 기능성에는 면역이 없다. 다른 기능성으로 인정받았다는 사실만 남긴다."""
    evidence = await source_with(lambda r: httpx.Response(500)).lookup(claim("비타민C", "면역력에 도움을"))

    assert evidence.recognition == "NOT_RECOGNIZED"
    assert evidence.other_recognitions


async def test_아연의_면역_기능_주장은_인정과_일치한다():
    evidence = await source_with(lambda r: httpx.Response(500)).lookup(claim("아연", "면역 기능에 도움을"))
    assert evidence.match_level == "EXACT"


# ── 전체 건 조회 ─────────────────────────────────────────────


async def test_인정_건을_전부_받아_뒤쪽의_맞는_건도_찾는다():
    """10건만 받을 때 프로바이오틱스 37건 중 뒤쪽 인정을 놓쳤다."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        rows = [row(f"다른균주{i}", "피부 보습에 도움을 줄 수 있음", f"2020-{i}") for i in range(12)]
        rows.append(row("장건강균주", "장 건강에 도움을 줄 수 있음", "2021-99"))
        return httpx.Response(200, json=payload(rows))

    evidence = await source_with(handler).lookup(claim("마늘", "장 건강에 도움을"))

    assert "/json/1/100/" in seen["url"]
    assert evidence.recognition_no == "2021-99"


# ── 복합물 ───────────────────────────────────────────────────


async def test_복합물의_인정은_단일_성분_주장과_완전_일치로_보지_않는다():
    """"포도블루베리추출혼합분말"의 인정이 블루베리 단일 성분 인정처럼 쓰였다."""
    rows = [row("포도블루베리추출혼합분말", "눈 건강에 도움을 줄 수 있음")]
    evidence = await source_with(lambda r: httpx.Response(200, json=payload(rows))).lookup(
        claim("블루베리", "눈 건강에 도움을")
    )

    assert evidence.recognition == "INDIVIDUAL"
    assert evidence.match_level == "PARTIAL"


async def test_단일_원료_인정이_있으면_복합물보다_먼저_쓴다():
    rows = [
        row("마늘등복합물", "간 건강에 도움을 줄 수 있음", "2019-1"),
        row("마늘추출물", "간 건강에 도움을 줄 수 있음", "2019-2"),
    ]
    evidence = await source_with(lambda r: httpx.Response(200, json=payload(rows))).lookup(
        claim("마늘", "간 건강에 도움을")
    )

    assert evidence.recognition_no == "2019-2"
    assert evidence.match_level == "EXACT"


# ── 캐시 ─────────────────────────────────────────────────────


async def test_같은_주장은_캐시로_답한다():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=payload([row("마늘추출물", "간 건강에 도움을 줄 수 있음")]))

    source = source_with(handler)
    await source.lookup(claim("마늘", "간 건강에 도움을"))
    await source.lookup(claim("마늘", "간 건강에 도움을"))

    assert len(calls) == 1
    assert source.cache_hits == 1


async def test_실패한_조회는_캐시하지_않는다():
    import pytest

    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500)
        return httpx.Response(200, json=payload([row("마늘추출물", "간 건강에 도움을 줄 수 있음")]))

    source = source_with(handler)
    with pytest.raises(Exception):
        await source.lookup(claim("마늘", "간 건강에 도움을"))
    await source.lookup(claim("마늘", "간 건강에 도움을"))

    assert len(calls) == 2


# ── 검증 실패가 섞인 판정 ────────────────────────────────────


async def test_검증_조회가_실패한_판정은_캐시하지_않는다():
    """근거가 빠진 판정이 굳으면 조회가 되살아난 뒤에도 같은 답을 준다."""
    from healthcheck.api.app import build_fake_orchestrator
    from healthcheck.pipeline import step1_input

    class BrokenAcademic:
        async def search(self, claim, **kwargs):
            raise RuntimeError("Liner 일시 장애")

    orchestrator = build_fake_orchestrator()
    orchestrator.academic = BrokenAcademic()
    share = step1_input.classify("홍삼이 면역력 증진에 도움을 줄 수 있다")

    result = await orchestrator.run(share)

    assert result.claim_checks and result.claim_checks[0].academic.lookup_failed
    assert await orchestrator.cached(share) is None
