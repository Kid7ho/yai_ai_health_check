"""브랜드와 전문가 역할 추출 테스트.

근거 화면에만 보여 주고 판정에는 쓰지 않는다. 실명은 뽑지 않는다.
"""

from __future__ import annotations

import pytest

from healthcheck.domain.labels import Label
from healthcheck.domain.types import CheckResult, ClaimSet, Verdict
from healthcheck.pipeline.step3_claims import RuleBasedClaimExtractor
from healthcheck.pipeline.step6_response import DISCLAIMER, find_forbidden, render, render_reason


@pytest.fixture(scope="module")
def extractor() -> RuleBasedClaimExtractor:
    return RuleBasedClaimExtractor()


# ── 브랜드 ───────────────────────────────────────────────────


def test_브랜드를_찾는다(extractor):
    assert extractor.extract("정관장 홍삼정이 면역력에 도움을 줍니다").brands == ("정관장",)


def test_다른_표기를_표준_브랜드로_모은다(extractor):
    """실제 뉴스에서 정관장과 KGC가 한 글에 함께 나왔다. 같은 브랜드다."""
    assert extractor.extract("KGC인삼공사의 정관장 제품").brands == ("정관장",)


def test_영문_브랜드는_단어_경계로_찾는다(extractor):
    assert extractor.extract("GNC 비타민이 인기입니다").brands == ("GNC",)
    assert extractor.extract("RGNCX 라는 코드").brands == ()


def test_브랜드가_없으면_비어_있다(extractor):
    assert extractor.extract("홍삼이 면역력에 도움을 줍니다").brands == ()


# ── 전문가 역할 ──────────────────────────────────────────────


def test_제목의_권위_표현을_찾는다(extractor):
    """실제 영상 제목 "의사가 알려주는 홍삼 궁합" 형태다."""
    assert extractor.extract("홍삼 먹고 계신 분들ㅣ의사가 알려주는 홍삼 궁합").authority_roles == ("의사",)


def test_한의사를_의사로_두_번_세지_않는다(extractor):
    assert extractor.extract("한의사가 추천하는 홍삼").authority_roles == ("한의사",)


@pytest.mark.parametrize("text", ["소비자의 의사결정에 도움", "환자와 의사소통이 중요", "박사과정 학생의 연구"])
def test_다른_뜻으로_쓰인_단어는_역할이_아니다(extractor, text):
    assert extractor.extract(text).authority_roles == ()


def test_여러_역할을_나온_순서대로_모은다(extractor):
    text = "약사가 설명하고 전문의가 덧붙이고 약사가 다시 정리합니다"
    assert extractor.extract(text).authority_roles == ("약사", "전문의")


def test_실명은_뽑지_않는다(extractor):
    roles = extractor.extract("김철수 박사가 말하는 홍삼").authority_roles
    assert roles == ("박사",)
    assert "김철수" not in "".join(roles)


# ── 근거 화면 표시 ───────────────────────────────────────────


def result_with(brands=(), roles=()) -> CheckResult:
    return CheckResult(
        verdict=Verdict(label=Label.CAUTION, rule_id="R-071", rules_version="v0.7", reasons=("설명", "근거")),
        claims=ClaimSet(claims=(), in_scope=True, disease_cure_expression=False, brands=brands, authority_roles=roles),
    )


def texts_of(payload):
    return [o["simpleText"]["text"] for o in payload["template"]["outputs"] if "simpleText" in o]


def test_근거_화면에_브랜드와_역할을_보여_준다():
    text = "\n".join(texts_of(render_reason(result_with(("정관장",), ("의사", "약사")))))

    assert "언급된 브랜드: 정관장" in text
    assert "언급된 전문가 역할: 의사, 약사" in text
    assert "확인하지 않았어요" in text


def test_근거_화면_표시에도_면책과_금지_표현_검사가_적용된다():
    texts = texts_of(render_reason(result_with(("정관장",), ("의사",))))
    assert DISCLAIMER in texts
    assert all(find_forbidden(text) == [] for text in texts)


def test_결과_카드에는_보여_주지_않는다():
    """근거 화면에만 보여 주기로 했다."""
    payload = render(result_with(("정관장",), ("의사",)))
    card = payload["template"]["outputs"][0]["textCard"]["description"]
    assert "정관장" not in card
    assert "의사" not in card


def test_없으면_표시하지_않는다():
    text = "\n".join(texts_of(render_reason(result_with())))
    assert "언급된" not in text


# ── 판정에는 쓰지 않는다 ─────────────────────────────────────


async def test_역할과_브랜드가_판정을_바꾸지_않는다():
    from healthcheck.api.app import build_fake_orchestrator
    from healthcheck.pipeline import step1_input

    plain = await build_fake_orchestrator().run(step1_input.classify("홍삼이 면역력 증진에 도움을 줄 수 있다"))
    framed = await build_fake_orchestrator().run(
        step1_input.classify("정관장 의사가 알려줍니다. 홍삼이 면역력 증진에 도움을 줄 수 있다")
    )

    assert framed.claims.brands == ("정관장",)
    assert framed.claims.authority_roles == ("의사",)
    assert framed.verdict.label is plain.verdict.label
