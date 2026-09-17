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


# ── 존댓말과 효능 동사 ───────────────────────────────────────
#
# 효능 표현을 요구하도록 바꾸자 가장 흔한 말투의 문장이 주장으로 잡히지 않았다.


@pytest.mark.parametrize(
    "text",
    [
        "홍삼이 면역력에 도움을 줍니다",
        "칼슘은 뼈 건강에 도움이 됩니다",
        "비타민 D는 뼈 건강에 좋습니다",
        "루테인을 먹으면 치매도 예방된다고 합니다",
        "녹차가 체지방 감소에 효과가 있습니다",
        "마늘이 혈압을 낮춰 줍니다",
        "생강이 염증을 억제한다고 해요",
        "오메가3가 중성지질을 줄여 줍니다",
    ],
)
def test_존댓말과_효능_동사도_주장으로_잡는다(extractor, text):
    assert extractor.extract(text).claims, text


def test_예방은_추측_표현과_함께여도_주장이다(extractor):
    claim = extractor.extract("루테인을 먹으면 치매도 예방된다고 합니다").claims[0]
    assert claim.ingredient == "루테인"
    assert "치매" in claim.claimed_effect


# ── 실제 링크 점검(2026-09-17): 질병 치료 표현 놓침과 글자 포함 오매칭 ─────────


@pytest.mark.parametrize(
    "text",
    [
        "매일 이 음료를 한잔 마시면 암이 사라집니다",
        "약사가 실제 섭취 중인 당뇨병을 없애주는 영양제",
        "이미 발생한 암세포를 죽이는 마늘의 항암 효과",
        "3개월 먹었을 뿐인데 관절염이 싹 사라지는 놀라운 음식",
    ],
)
def test_활용형이_다른_질병_치료_표현도_질병_이름과_함께면_잡는다(extractor, text):
    """목록이 '사라진다'처럼 한 활용형만 담아 실제 영상 제목을 놓쳤다."""
    assert extractor.extract(text).disease_cure_expression is True


@pytest.mark.parametrize(
    "text",
    [
        "홍삼을 먹으면 피로가 싹 사라집니다",
        "단순 영양제보다 낫습니다. 신경과 의사들이 챙겨 먹는 치매 예방 음식",
        "그 제품은 내 류마티스를 고쳐주기는커녕 증상을 키웠다",
    ],
)
def test_질병이_아니거나_비교·부정이면_질병_치료_표현이_아니다(extractor, text):
    assert extractor.extract(text).disease_cure_expression is False


def test_긴_글의_성분_없는_제목에_있는_완치_표현도_잡는다(extractor):
    """긴 글은 성분 주변 문장만 읽어 제목의 '당뇨 완치'를 놓쳤다."""
    body = " ".join(["여주는 쓴맛이 강한 채소로 여름철에 많이 먹습니다."] * 60)
    text = f"당뇨 완치 환자들이 먹은 이것. {body}"
    assert len(text) > 1500
    assert extractor.extract(text).disease_cure_expression is True


def test_동사_안의_여주는_성분으로_보지_않는다(extractor):
    """크릴오일 기사의 '콜레스테롤을 줄여주고'에서 여주 주장이 생겼다."""
    claims = extractor.extract("크릴오일은 나쁜 콜레스테롤을 줄여주고 혈관 건강에 도움을 줍니다").claims
    assert "여주" not in {claim.ingredient for claim in claims}
    assert "여주" in {c.ingredient for c in extractor.extract("여주가 혈당을 낮추는 데 도움을 줍니다").claims}


def test_마약_끊는_이야기는_약_중단_권유가_아니다(extractor):
    """관절 영상 자막의 '마약 끊어내는 과정'이 즉시 거절에 걸렸다."""
    assert extractor.extract("마치 마약 끊어내는 과정처럼 단맛을 끊어내세요").in_scope is True
    assert extractor.extract("혈압약 끊고 마늘즙만 드세요").in_scope is False
