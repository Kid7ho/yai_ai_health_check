"""긴 글의 범위 판단과 출처 목록.

실채널에서 공유된 홍삼 연구 소개 블로그가 의료 단어가 섞인 문장 두 개
("기존의 호르몬 치료나 수술적 치료는", "항생제 투여로 … 마우스 모델") 때문에
글 전체가 범위 밖으로 거절됐다. 글 끝의 출처 목록은 기사 제목들이 한 주장으로 묶였다.
"""

from __future__ import annotations

import pytest

from healthcheck.pipeline.step3_claims import RuleBasedClaimExtractor


@pytest.fixture(scope="module")
def extractor() -> RuleBasedClaimExtractor:
    return RuleBasedClaimExtractor()


def test_의료_단어가_섞인_문장만_빼고_나머지_주장을_검증한다(extractor):
    text = (
        "기존의 수술 치료는 부담이 커서 홍삼이 통증 완화에 도움을 준다는 연구가 주목받고 있습니다.\n"
        "홍삼이 면역력 증진에 도움을 줄 수 있습니다."
    )

    result = extractor.extract(text)

    assert result.in_scope is True
    assert [(c.ingredient, "면역" in c.claimed_effect) for c in result.claims] == [("홍삼", True)]


def test_주장이_의료_단어_문장에만_있으면_거절한다(extractor):
    result = extractor.extract("항생제 대신 쓰려고 홍삼이 염증 완화에 도움을 준다고 합니다.")

    assert result.in_scope is False


def test_대체를_권하는_표현은_긴_글에서도_바로_거절한다(extractor):
    text = "홍삼이 면역력 증진에 도움을 줄 수 있습니다.\n그러니 당뇨약 끊고 홍삼만 드세요."

    assert extractor.extract(text).in_scope is False


def test_출처_목록의_기사_제목은_주장으로_읽지_않는다(extractor):
    text = (
        "홍삼이 면역력 증진에 도움을 줄 수 있습니다.\n"
        "<홍삼, 장 건강 개선에 효과>, 신문A <홍삼이 피로 회복에 도움>, 신문B\n"
        "#홍삼 #장건강 #피로회복"
    )

    result = extractor.extract(text)

    assert len(result.claims) == 1
    assert "면역" in result.claims[0].claimed_effect


def test_출처로_시작하는_줄도_읽지_않는다(extractor):
    text = "홍삼이 면역력 증진에 도움을 줄 수 있습니다.\n출처: 홍삼이 기억력 개선에 도움을 준다는 기사"

    assert len(extractor.extract(text).claims) == 1


def test_출처_줄을_비워도_주장_위치는_원문과_맞는다(extractor):
    text = (
        "<홍삼, 장 건강 개선에 효과>, 신문A <홍삼이 피로 회복에 도움>, 신문B\n"
        "그리고 홍삼이 면역력 증진에 도움을 줄 수 있습니다."
    )

    claim = extractor.extract(text).claims[0]
    start, end = claim.span

    assert text[start:end] == "홍삼"
    assert start > text.index("\n")
