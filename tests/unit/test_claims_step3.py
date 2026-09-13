"""③ 주장 추출 점검에서 찾은 결함 테스트.

실제 블로그·뉴스·자막 8건으로 확인한 결함을 고정한다.
"""

from __future__ import annotations

import pytest

from healthcheck.domain.types import Claim
from healthcheck.pipeline.step3_claims import RuleBasedClaimExtractor


@pytest.fixture(scope="module")
def extractor() -> RuleBasedClaimExtractor:
    return RuleBasedClaimExtractor()


# ── 부당광고 오탐 ───────────────────────────────────────────


def test_부작용을_조심하라는_조언은_부당광고가_아니다(extractor):
    """실제 블로그 소제목이 주의하세요 판정을 받았다."""
    assert extractor.extract("홍삼 부작용 걱정 없이 안전하게 섭취하는 방법").prohibited_ad_expression is False


def test_제품을_부작용_없다고_꾸미면_부당광고다(extractor):
    assert extractor.extract("부작용 걱정 없는 천연 홍삼입니다").prohibited_ad_expression is True


# ── 효능 표현이 없는 짝 ──────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["홍삼은 눈과 뼈 이야기에도 자주 나옵니다", "오늘은 홍삼과 고혈압에 대해 알아보겠습니다"],
)
def test_효능_표현이_없으면_주장이_아니다(extractor, text):
    """실제 자막에서 "홍삼 / 눈", "홍삼 / 뼈" 같은 짝이 10건 가까이 잡혔다."""
    assert extractor.extract(text).claims == ()


def test_효능_표현이_있으면_주장이다(extractor):
    assert len(extractor.extract("홍삼이 면역력 증진에 도움을 줄 수 있다").claims) == 1


def test_주장이_없어도_질병_치료_표현은_잡는다(extractor):
    assert extractor.extract("이 즙 드시면 당뇨가 낫습니다").disease_cure_expression is True


# ── 성분 사전 확장 ───────────────────────────────────────────


@pytest.mark.parametrize(
    "text,ingredient",
    [
        ("비타민C가 면역력에 도움을 줍니다", "비타민C"),
        ("비타민 D는 뼈 건강에 좋습니다", "비타민D"),
        ("아연이 면역 기능에 도움을 줍니다", "아연"),
        ("칼슘은 뼈 건강에 도움이 됩니다", "칼슘"),
        ("코큐텐이 피로 개선에 좋다고 합니다", "코엔자임Q10"),
        ("카테킨이 체지방 감소에 도움을 줍니다", "녹차"),
        ("인삼이 피로 개선에 좋습니다", "인삼"),
        ("아로니아가 눈 건강에 좋다고 합니다", "아로니아"),
        ("생강차가 혈액순환에 좋다고 합니다", "생강"),
    ],
)
def test_흔한_원료를_찾는다(extractor, text, ingredient):
    """실제 본문에 나온 원료 14종 중 4종만 잡혔다."""
    claims = extractor.extract(text).claims
    assert claims and claims[0].ingredient == ingredient


def test_인삼을_홍삼과_합치지_않는다(extractor):
    """가공 방식과 인정 기능성이 다를 수 있어 따로 둔다. 자문의 판단 대기."""
    assert extractor.extract("인삼이 피로 개선에 좋습니다").claims[0].ingredient != "홍삼"


def test_새_원료도_영문_검색어가_있다():
    from healthcheck.adapters.liner.academic import build_query

    claim = Claim(ingredient="비타민C", claimed_effect="면역력에 도움을", intensity="SUGGESTIVE", span=(0, 4))
    assert build_query(claim) == "vitamin C immune function"
