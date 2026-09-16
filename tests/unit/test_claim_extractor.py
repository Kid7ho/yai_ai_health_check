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


def test_반박_문장을_오정보로_읽지_않는다(extractor):
    """한계 4번이었다. 부정 표현 처리로 해결했다.

    오정보를 바로잡는 영상이 오정보 판정을 받는 것은 판정 방향이 뒤집히는
    오판이었다. 보수적 기본값 원칙과도 충돌했다.
    """
    text = "블루베리가 당뇨를 완치시킨다는 말은 근거가 없습니다"
    result = extractor.extract(text)

    assert result.disease_cure_expression is False


@pytest.mark.parametrize(
    "text",
    [
        "홍삼이 만병통치약은 아니며, 약물 복용 시 주의가 필요합니다",
        "홍삼이 만병통치약처럼 팔리지만, 솔직히 그건 과장입니다",
        "이건 홍삼을 먹으면 암이 낫는다는 얘기가 절대 아니에요",
        "블루베리로 당뇨를 고친다는 건 잘못된 정보입니다",
    ],
)
def test_실제_블로그의_반박_문장들(extractor, text):
    """전부 실제 블로그 본문에서 WARNING을 유발했던 문장이다."""
    assert extractor.extract(text).disease_cure_expression is False


def test_부정_표현이_없으면_그대로_잡는다(extractor):
    """부정문 처리가 진짜 주장까지 삼키면 안 된다."""
    assert extractor.extract("홍삼을 드시면 당뇨가 완치됩니다").disease_cure_expression is True


@pytest.mark.xfail(reason="한계 5: 문장 부호가 없는 자막은 전체가 한 문장이 된다", strict=True)
def test_한계_문장_부호_없는_자막(extractor):
    text = "안녕하세요 오늘은 홍삼에 대해 알아보겠습니다 그런데 요즘 당뇨가 낫는다는 영상이 많아요"
    result = extractor.extract(text)
    # 홍삼과 당뇨는 서로 관계없는 문맥인데 한 주장으로 묶인다.
    assert not any(
        claim.ingredient == "홍삼" and "당뇨" in claim.claimed_effect
        for claim in result.claims
    )


# ─────────────────────────────────────────────────────────────
# 긴 글 처리 — 실제 블로그 본문에서 드러난 문제들
# ─────────────────────────────────────────────────────────────

LONG_PREFIX = "요즘 건강에 관심이 많아졌습니다. " * 40  # 성분 이야기가 없는 구간


def test_긴_글에서_성분_언급_주변만_본다(extractor):
    """4천 자 글에서 성분과 무관한 구간까지 훑으면 엉뚱한 짝이 생긴다."""
    text = LONG_PREFIX + "그래서 홍삼이 면역력 증진에 도움을 준다고 합니다. " + LONG_PREFIX
    result = extractor.extract(text)

    assert len(result.claims) == 1
    assert result.claims[0].ingredient == "홍삼"
    assert "면역력" in result.claims[0].claimed_effect


def test_성분을_못_찾은_긴_글은_전체를_본다(extractor):
    """성분명 사전이 좁아 못 찾은 경우다. 글을 통째로 버리면 질병 표현도 놓친다."""
    text = LONG_PREFIX + "이 즙을 드시면 당뇨가 낫습니다. " + LONG_PREFIX
    result = extractor.extract(text)

    assert result.disease_cure_expression is True


def test_질병_표현은_성분_효능_문장에서만_인정한다(extractor):
    """4천 자 홍삼 가이드 글에 '완치'가 한 번 나온 것만으로 주의하세요가 됐다."""
    text = (
        "홍삼은 면역력 증진에 도움을 줄 수 있습니다. "
        "건강정보를 볼 때 완치라는 표현은 조심해서 봐야 합니다."
    )
    result = extractor.extract(text)

    assert result.disease_cure_expression is False


def test_성분과_질병이_같은_문장이면_질병_표현으로_본다(extractor):
    result = extractor.extract("홍삼을 드시면 당뇨가 완치됩니다")
    assert result.disease_cure_expression is True


def test_치료제라는_명사는_질병_치료_표현이_아니다(extractor):
    """실제 글의 '마이크로바이옴 치료제'가 걸려 식품 글이 주의하세요가 됐다."""
    text = "프로바이오틱스가 마이크로바이옴 치료제로 등급이 올라간 사례가 있습니다."
    assert extractor.extract(text).disease_cure_expression is False


def test_병을_치료한다는_서술은_잡는다(extractor):
    assert extractor.extract("홍삼이 당뇨를 치료한다").disease_cure_expression is True


# ─────────────────────────────────────────────────────────────
# 부당광고 표현 검사
#
# 식약처 적발 기록 조회가 아니다. 전용 오픈API를 찾지 못해서, 법령이 금지하는
# 표현 유형에 해당하는지 문장에서 확인하는 방식으로 대체했다.
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "홍삼은 부작용이 전혀 없어서 누구나 드실 수 있습니다",
        "이 제품은 식약처 공식 인증을 받았습니다",
        "천연 항생제라고 불리는 마늘입니다",
        "하루 만에 효과를 느끼실 수 있습니다",
        "약보다 효과가 좋습니다",
    ],
)
def test_법으로_금지된_광고_표현을_찾아낸다(extractor, text):
    assert extractor.extract(text).prohibited_ad_expression is True


def test_부정문_안의_금지_표현은_세지_않는다(extractor):
    """오정보를 바로잡는 글이 부당광고로 판정되면 안 된다."""
    assert (
        extractor.extract("부작용이 전혀 없다는 말은 거짓입니다").prohibited_ad_expression
        is False
    )


def test_정상적인_기능성_표현은_금지_표현이_아니다(extractor):
    text = "홍삼이 면역력 증진에 도움을 줄 수 있다고 알려져 있습니다"
    assert extractor.extract(text).prohibited_ad_expression is False


def test_금지_표현은_질병_치료_표현과_별개로_기록한다(extractor):
    """규칙 R-021과 R-031이 각각 다른 값을 본다."""
    result = extractor.extract("홍삼은 부작용이 전혀 없습니다")
    assert result.prohibited_ad_expression is True
    assert result.disease_cure_expression is False


def test_개인_체험기는_부당광고로_보지_않는다(extractor):
    """광고가 아닌 시민의 공유 글에도 흔히 나오는 형태다.

    어르신이 지인에게 받는 메시지 상당수가 이런 후기다. 그것을 부당광고로
    판정하면 광고가 아닌 글을 위반으로 표시하게 된다.
    """
    result = extractor.extract("저는 홍삼 먹고 컨디션이 좋아졌습니다")
    assert result.prohibited_ad_expression is False


def test_체험기가_질병_완치를_말하면_질병_표현으로_잡힌다(extractor):
    """부당광고 목록에서 빼도 위험한 형태는 놓치지 않는다."""
    result = extractor.extract("저는 홍삼 먹고 당뇨가 나았습니다")
    assert result.prohibited_ad_expression is False
    assert result.disease_cure_expression is True


# ── 긴 글: 성분·효능 대상·효능 표현의 거리 ────────────────────


def test_한_문장에_여러_이야기가_있으면_가까운_짝만_주장으로_본다(extractor):
    """실측(헬스조선 기사): '관절염 환자 79명이 40개월 동안 하루 4g의 동결건조 블루베리
    분말을'이 효능 문구로 잡혀 식약처 대조와 학술 검색어가 엉뚱해졌다."""
    text = "관절염 환자 79명이 40개월 동안 하루 4g의 동결건조 블루베리 분말을 먹었더니 통증이 줄었다."

    claims = extractor.extract(text).claims

    for claim in claims:
        assert len(claim.claimed_effect) <= 30
        assert "79명" not in claim.claimed_effect


def test_효능_대상이_없으면_주장으로_세지_않는다(extractor):
    """'몸에 좋다'만으로는 무엇에 좋다는 것인지 검증할 수 없다."""
    assert extractor.extract("홍삼이 몸에 아주 좋습니다").claims == ()


def test_효능_문구는_효능_대상부터_담는다(extractor):
    claim = extractor.extract("오메가3 지방산은 관절 근골격계 통증과 뻣뻣함을 줄이는 데 좋다").claims[0]

    assert claim.ingredient == "오메가3"
    assert claim.claimed_effect.startswith("관절")
    assert len(claim.claimed_effect) <= 30


def test_효능_대상이_표현_앞에_있는_짝을_먼저_고른다(extractor):
    """실측(헬스조선 홍삼 기사): 뒤쪽 대상과 짝지어 '개선하고'만 남았다."""
    text = "홍삼이 노화로 인한 면역력 저하를 개선하고 면역 기능을 건강하게 유지하는데 도움을 준다"

    assert extractor.extract(text).claims[0].claimed_effect == "면역력 저하를 개선하고"


def test_제목_줄에서도_대상과_표현을_붙여_읽는다(extractor):
    """'혈전 억제·면역력 개선'에서 '억제·면역력'이 효능 문구로 잡혔다."""
    text = "홍삼, 미국서도 효능 입증… 혈전 억제·면역력 개선 효과 밝혀져"

    assert extractor.extract(text).claims[0].claimed_effect == "면역력 개선"
