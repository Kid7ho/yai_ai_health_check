"""③ 주장 구조화 단위 테스트.

앞쪽은 규칙 기반 추출이 잡아내는 문장이고, 뒤쪽 '알려진 한계'는 놓치는
문장을 xfail로 기록한 것이다. 한계를 주석에만 적으면 잊힌다. 테스트 목록에
남겨 두면 목록만 봐도 어디가 새는지 드러나고, 나중에 개선했을 때
xpass로 바뀌어 바로 알 수 있다.
"""

from __future__ import annotations

import pytest

from healthcheck.pipeline.step3_claims import (
    LlmClaimExtractor,
    RuleBasedClaimExtractor,
)


@pytest.fixture(scope="module")
def extractor() -> RuleBasedClaimExtractor:
    return RuleBasedClaimExtractor()


# ─────────────────────────────────────────────────────────────
# 잡히는 문장
# ─────────────────────────────────────────────────────────────


def test_성분과_효능이_한_문장에_있으면_뽑아낸다(extractor):
    result = extractor.extract("냉동 블루베리가 당뇨를 완치시킨다")
    assert len(result.claims) == 1
    claim = result.claims[0]
    assert claim.ingredient == "블루베리"
    assert "당뇨" in claim.claimed_effect
    assert claim.intensity == "ASSERTIVE"


def test_사전에_등록된_다른_표기도_표준_성분명으로_바꾼다(extractor):
    result = extractor.extract("유산균이 장에 좋다")
    assert result.claims[0].ingredient == "프로바이오틱스"
    assert result.claims[0].raw_ingredient == "유산균"


def test_긴_표기가_짧은_표기에_먹히지_않는다(extractor):
    """'6년근 홍삼'을 '홍삼'으로 두 번 잡으면 중복 주장이 생긴다."""
    result = extractor.extract("6년근 홍삼 먹고 혈압 좋아졌어요")
    assert [claim.ingredient for claim in result.claims] == ["홍삼"]


def test_질병_치료_표현을_따로_표시한다(extractor):
    """규칙 R-021이 이 값만 보고 WARNING으로 보낸다."""
    assert extractor.extract("이 즙 드시면 당뇨가 낫습니다").disease_cure_expression is True
    assert extractor.extract("홍삼이 면역력에 도움이 된다").disease_cure_expression is False


def test_완치_표현은_단정으로_읽는다(extractor):
    """'완치시킨다'를 완화 표현으로 읽으면 표현 강도가 거꾸로 기록된다."""
    assert extractor.extract("블루베리가 당뇨를 완치시킨다").claims[0].intensity == "ASSERTIVE"


@pytest.mark.parametrize(
    "text",
    [
        "당뇨약 끊고 이 즙만 드세요",
        "인슐린 안 맞아도 됩니다",
        "수술 대신 이걸 드세요",
        "병원 검사 결과보다 정확합니다",
    ],
)
def test_의약품_시술_진단_이야기는_범위_밖으로_표시한다(extractor, text):
    """규칙 R-001이 이 값만 보고 판정을 거절한다."""
    assert extractor.extract(text).in_scope is False


def test_성분_이름만_나온_문장은_주장이_아니다(extractor):
    result = extractor.extract("어제 홍삼을 샀습니다")
    assert result.claims == ()
    assert result.extraction_failed is True


def test_성분을_못_찾으면_추출_실패로_표시한다(extractor):
    """추출 실패는 오류가 아니다. 판정 보류로 이어지는 정상 경로다."""
    result = extractor.extract("이걸 드시면 좋아집니다")
    assert result.extraction_failed is True


def test_여러_성분이_나오면_모두_뽑는다(extractor):
    result = extractor.extract("홍삼은 면역력에 좋고 루테인은 눈에 좋다")
    assert {claim.ingredient for claim in result.claims} == {"홍삼", "루테인"}


def test_빈_텍스트도_예외를_던지지_않는다(extractor):
    result = extractor.extract("")
    assert result.claims == ()
    assert result.extraction_failed is True


def test_LLM_추출기는_자리만_비어_있다():
    """인터페이스만 두고 구현하지 않는다. 규칙 기반 한계를 측정한 뒤 정한다."""
    with pytest.raises(NotImplementedError):
        LlmClaimExtractor().extract("아무 문장")


# ─────────────────────────────────────────────────────────────
# 알려진 한계 — 규칙 기반 추출이 놓치는 문장
#
# 이 테스트들은 지금 실패한다. 실패 자체가 기록이다.
# 발표 자료의 "③ 구조화는 규칙 기반" 문구를 수정할 근거가 여기 있다.
# ─────────────────────────────────────────────────────────────


@pytest.mark.xfail(reason="한계 1: 성분과 효능이 다른 문장에 흩어져 있으면 짝짓지 못한다", strict=True)
def test_한계_문장을_넘나드는_주장(extractor):
    text = "제가 매일 먹는 게 있어요. 그게 홍삼입니다. 이걸 먹고 혈압이 내려갔어요."
    result = extractor.extract(text)
    assert result.claims and result.claims[0].ingredient == "홍삼"


@pytest.mark.xfail(reason="한계 2: 대명사로 성분을 지칭하면 사전 매칭이 실패한다", strict=True)
def test_한계_대명사_지칭(extractor):
    result = extractor.extract("이걸 꾸준히 드시면 당뇨가 낫습니다")
    assert result.claims, "성분을 특정하지 못해 주장이 비어 있다"


@pytest.mark.xfail(reason="한계 3: 비유·간접 표현은 효능 대상 목록에 없다", strict=True)
def test_한계_비유_표현(extractor):
    result = extractor.extract("마늘을 먹으면 혈관이 뻥 뚫립니다")
    assert result.claims, "'혈관이 뚫린다'를 효능으로 읽지 못한다"


@pytest.mark.xfail(
    reason="한계 4: 오정보를 반박하는 문장을 오정보로 오인한다. 판정 방향이 뒤집히는 가장 위험한 한계",
    strict=True,
)
def test_한계_반박_문장_오인(extractor):
    text = "블루베리가 당뇨를 완치시킨다는 말은 근거가 없습니다"
    result = extractor.extract(text)
    # 반박하는 영상인데 질병 치료 표현이 잡혀 WARNING으로 간다.
    assert result.disease_cure_expression is False


@pytest.mark.xfail(reason="한계 5: 문장 부호가 없는 자막은 전체가 한 문장이 된다", strict=True)
def test_한계_문장_부호_없는_자막(extractor):
    text = "안녕하세요 오늘은 홍삼에 대해 알아보겠습니다 그런데 요즘 당뇨가 낫는다는 영상이 많아요"
    result = extractor.extract(text)
    # 홍삼과 당뇨는 서로 관계없는 문맥인데 한 주장으로 묶인다.
    assert not any(
        claim.ingredient == "홍삼" and "당뇨" in claim.claimed_effect
        for claim in result.claims
    )
