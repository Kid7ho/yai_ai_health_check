"""식약처 실제 클라이언트 단위 테스트. 네트워크를 타지 않는다."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from healthcheck.adapters.mfds.foodsafetykorea import (
    FoodSafetyKoreaDataSource,
    MfdsLookupError,
    match_level,
)
from healthcheck.domain.types import Claim

NOTIFIED_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "notified_minimal.yaml"

GARLIC = Claim(ingredient="마늘", claimed_effect="혈압을 낮추는 데 좋다고", intensity="SUGGESTIVE", span=(0, 2))
GINSENG = Claim(ingredient="홍삼", claimed_effect="면역력 증진에 도움을", intensity="SUGGESTIVE", span=(0, 2))
GINSENG_CURE = Claim(ingredient="홍삼", claimed_effect="당뇨를 완치시킨다", intensity="ASSERTIVE", span=(0, 2))

# sample 키 실제 응답 형식을 그대로 따른다.
def payload(rows: list[dict], code: str = "INFO-000") -> dict:
    return {
        "I-0040": {
            "total_count": str(len(rows)),
            "row": rows,
            "RESULT": {"CODE": code, "MSG": "정상처리되었습니다."},
        }
    }


ROW_LIVER = {
    "APLC_RAWMTRL_NM": "흑마늘추출물",
    "FNCLTY_CN": "“간 건강에 도움을 줄 수 있음” (기타기능II)",
    "HF_FNCLTY_MTRAL_RCOGN_NO": "2016-16",
}
ROW_BP = {
    "APLC_RAWMTRL_NM": "마늘추출물",
    "FNCLTY_CN": "높은 혈압 개선에 도움을 줄 수 있음",
    "HF_FNCLTY_MTRAL_RCOGN_NO": "2018-3",
}


def source_with(handler, **kwargs) -> FoodSafetyKoreaDataSource:
    kwargs.setdefault("notified_path", NOTIFIED_FIXTURE)
    return FoodSafetyKoreaDataSource(
        "test-key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs
    )


# ── 고시형 목록이 먼저다 ─────────────────────────────────────


async def test_고시형_원료는_API를_호출하지_않는다():
    """I-0040은 개별인정형만 담는다. 고시형은 공전 기반 로컬 목록에 있다."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("고시형 성분에 API를 호출했다")

    source = source_with(handler)
    evidence = await source.lookup(GINSENG)

    assert evidence.recognition == "NOTIFIED"
    assert evidence.match_level == "EXACT"
    assert source.call_count == 0


async def test_고시형_목록에_없으면_API를_조회한다():
    source = source_with(lambda request: httpx.Response(200, json=payload([ROW_BP])))
    evidence = await source.lookup(GARLIC)

    assert evidence.recognition == "INDIVIDUAL"
    assert evidence.recognition_no == "2018-3"
    assert source.call_count == 1


# ── 응답 해석 ────────────────────────────────────────────────


async def test_데이터가_없으면_미인정으로_본다():
    """INFO-200은 오류가 아니다."""
    source = source_with(lambda request: httpx.Response(200, json=payload([], code="INFO-200")))
    evidence = await source.lookup(GARLIC)

    assert evidence.recognition == "NOT_RECOGNIZED"
    assert evidence.lookup_failed is False


async def test_인정_건이_여러_개면_주장과_가장_잘_맞는_문구를_고른다():
    """임의의 한 건을 고르면 판정이 우연에 좌우된다."""
    source = source_with(lambda request: httpx.Response(200, json=payload([ROW_LIVER, ROW_BP])))
    evidence = await source.lookup(GARLIC)

    assert evidence.recognition_no == "2018-3"
    assert evidence.match_level == "EXACT"


async def test_기능성_문구의_따옴표와_등급_표기를_정리한다():
    liver_claim = Claim(ingredient="마늘", claimed_effect="간에 좋다", intensity="SUGGESTIVE", span=(0, 2))
    source = source_with(lambda request: httpx.Response(200, json=payload([ROW_LIVER])))

    evidence = await source.lookup(liver_claim)

    assert evidence.functionality_text.startswith("간 건강")


async def test_주장_효능에_대한_인정이_없으면_미인정으로_본다():
    """인정 여부는 성분 하나가 아니라 (성분, 효능) 쌍에 대한 것이다.

    "마늘"이 간 건강으로 인정됐다는 사실은 혈압 주장의 근거가 아니다.
    신청원료명 부분 검색이 다른 제품 형태의 인정까지 끌어오기 때문에,
    이 처리가 없으면 마늘·혈압 주장이 R-060의 WARNING으로 갔다.
    """
    source = source_with(lambda request: httpx.Response(200, json=payload([ROW_LIVER])))

    evidence = await source.lookup(GARLIC)

    assert evidence.recognition == "NOT_RECOGNIZED"
    assert evidence.match_level == "NOT_APPLICABLE"
    assert evidence.functionality_text is None
    # 다른 효능으로 인정받은 사실은 설명용으로 남긴다.
    assert evidence.other_recognitions and "간 건강" in evidence.other_recognitions[0]


async def test_부당광고_정보는_아직_없으므로_항상_False다():
    """전용 API를 특정하지 못했다. 데이터를 지어내지 않는다."""
    source = source_with(lambda request: httpx.Response(200, json=payload([ROW_BP])))
    assert (await source.lookup(GARLIC)).ad_violation_hit is False


# ── 실패 처리 ────────────────────────────────────────────────


async def test_인증키가_무효면_예외를_올린다():
    """INFO-100은 인증키 무효다. 미인정과 구분해야 한다."""
    source = source_with(
        lambda request: httpx.Response(
            200,
            json={"I-0040": {"RESULT": {"CODE": "INFO-100", "MSG": "인증키가 유효하지 않습니다."}}},
        )
    )
    with pytest.raises(MfdsLookupError, match="INFO-100"):
        await source.lookup(GARLIC)


async def test_통신_오류는_예외를_올린다():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결 실패")

    with pytest.raises(MfdsLookupError):
        await source_with(handler).lookup(GARLIC)


async def test_조회_실패는_판정_단계에서_UNKNOWN으로_흡수된다():
    from healthcheck.adapters.fake.academic import FakeAcademicSearchClient
    from healthcheck.pipeline.step4_verify import verify

    source = source_with(lambda request: httpx.Response(500, text="error"))
    _, mfds = await verify(GARLIC, academic=FakeAcademicSearchClient(), mfds=source)

    assert mfds.lookup_failed is True
    assert mfds.recognition == "UNKNOWN"


def test_키가_없으면_클라이언트를_만들_수_없다():
    with pytest.raises(ValueError, match="MFDS_API_KEY"):
        FoodSafetyKoreaDataSource("")


# ── 일치도 판정 ──────────────────────────────────────────────


def test_인정_문구에_없는_효능은_불일치다():
    level = match_level(GINSENG_CURE, "면역력 증진에 도움을 줄 수 있음", [])
    assert level == "MISMATCH"


def test_단정_표현은_완전_일치로_보지_않는다():
    """인정 문구는 모두 '도움을 줄 수 있음'이라는 완화 표현이다."""
    assertive = Claim(ingredient="홍삼", claimed_effect="면역력이 확실히 좋아진다", intensity="ASSERTIVE", span=(0, 2))
    assert match_level(assertive, "면역력 증진에 도움을 줄 수 있음", []) == "PARTIAL"


def test_효능_대상을_특정하지_못하면_부분_일치로_둔다():
    """비교할 수 없는 것을 완전 일치로 보면 TRUSTWORTHY가 새어 나간다."""
    vague = Claim(ingredient="홍삼", claimed_effect="몸에 좋다", intensity="SUGGESTIVE", span=(0, 2))
    assert match_level(vague, "면역력 증진에 도움을 줄 수 있음", []) == "PARTIAL"
