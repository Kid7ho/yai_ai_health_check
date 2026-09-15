"""PC 카카오톡 캡처 점검에서 찾은 결함.

  · "확인한 내용"이 "홍삼이(가) 면역력 증진에"로 조사가 어색하고 끝이 잘렸다
  · 근거에 영어 논문 제목이 그대로 나왔다
  · "Rg3 홍삼이 고혈압 쥐의 혈관 기능을 개선" 논문이 면역력 주장의 뒷받침으로 세어졌다
  · 출처에 "고시형" 같은 행정 용어가 나왔다
"""

from __future__ import annotations

import pytest

from healthcheck.adapters.liner.academic import effect_in_title, is_relevant
from healthcheck.domain.labels import Label
from healthcheck.domain.types import AcademicEvidence, CheckResult, Claim, MfdsEvidence, Paper, Verdict
from healthcheck.pipeline.step3_claims import RuleBasedClaimExtractor
from healthcheck.pipeline.step6_response import _subject_particle, render, render_reason


@pytest.fixture(scope="module")
def extractor() -> RuleBasedClaimExtractor:
    return RuleBasedClaimExtractor()


# ── 확인한 내용 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("홍삼이 면역력 증진에 도움을 줄 수 있다", "홍삼이 면역력 증진에 도움을 줄 수 있다"),
        # "냉동 블루베리"는 성분 사전의 표기 그대로다.
        ("냉동 블루베리가 당뇨를 완치시킨다", "냉동 블루베리가 당뇨를 완치시킨다"),
        ("마늘이 혈압을 낮추는 데 좋다고 합니다", "마늘이 혈압을 낮추는 데 좋다고 합니다"),
    ],
)
def test_확인한_내용은_원문_구절을_그대로_보여_준다(extractor, text, expected):
    assert extractor.extract(text).claims[0].statement == expected


def test_구절은_문장부호에서_끊고_60자를_넘지_않는다(extractor):
    text = "홍삼이 면역력 증진에 도움을 줄 수 있다, 그리고 " + "아주 " * 40 + "좋습니다."
    statement = extractor.extract(text).claims[0].statement
    assert statement == "홍삼이 면역력 증진에 도움을 줄 수 있다"
    long = extractor.extract("홍삼이 " + "정말 " * 30 + "면역력 증진에 도움을 줍니다").claims[0].statement
    assert len(long) <= 60


def test_카드에_원문_구절이_나간다(extractor):
    claims = extractor.extract("홍삼이 면역력 증진에 도움을 줄 수 있다")
    result = CheckResult(
        verdict=Verdict(label=Label.TRUSTWORTHY, rule_id="R-040", rules_version="t", reasons=()),
        claims=claims,
        primary_claim=claims.claims[0],
    )
    card = render(result, image_base_url="https://example.com")["template"]["outputs"][0]["basicCard"]
    assert card["description"].split("\n\n")[0] == "홍삼이 면역력 증진에 도움을 줄 수 있다"
    assert "이(가)" not in card["description"]


@pytest.mark.parametrize(
    ("word", "particle"),
    [("홍삼", "이"), ("블루베리", "가"), ("루테인", "이"), ("오메가3", "이"), ("비타민C", "이(가)")],
)
def test_구절이_없으면_받침을_보고_조사를_고른다(word, particle):
    assert _subject_particle(word) == particle


# ── 근거 화면 ────────────────────────────────────────────────


def reason_result() -> CheckResult:
    papers = (
        Paper(title="Korean red ginseng enhances antiviral immunity", url="https://paper.example/1", stance="SUPPORT"),
        Paper(title="Immune Activity of Polysaccharide Fractions", url="https://paper.example/2", stance="SUPPORT"),
    )
    return CheckResult(
        verdict=Verdict(
            label=Label.TRUSTWORTHY, rule_id="R-040", rules_version="t", reasons=(),
            guidance="나라가 인정한 기능성과 같고, 이를 뒷받침하는 연구도 있어요.",
        ),
        academic=AcademicEvidence(supporting=2, refuting=0, papers=papers),
        mfds=MfdsEvidence(recognition="NOTIFIED", functionality_text="면역력 증진에 도움을 줄 수 있음", match_level="EXACT"),
    )


def test_근거에는_영어_제목_대신_편수를_보여_준다():
    for payload in (render_reason(reason_result()), render_reason(reason_result(), image_base_url="https://example.com")):
        text = "\n".join(
            o.get("simpleText", {}).get("text") or o.get("basicCard", {}).get("description", "")
            for o in payload["template"]["outputs"]
        )
        assert "2편 중 뒷받침 2편, 반박 0편" in text
        assert "immunity" not in text and "Polysaccharide" not in text


def test_행정_용어_대신_쉬운_말을_쓴다():
    text = render_reason(reason_result())["template"]["outputs"][0]["simpleText"]["text"]
    assert "고시형" not in text
    assert "나라가 정한 기능성 원료" in text


def test_근거_카드에_연구_원문_링크_버튼이_있다():
    card = render_reason(reason_result(), image_base_url="https://example.com")["template"]["outputs"][0]["basicCard"]
    assert card["buttons"] == [
        {"action": "webLink", "label": "연구 자세히 보기", "webLinkUrl": "https://paper.example/1"}
    ]


def test_연구가_없으면_링크_버튼을_두지_않는다():
    result = reason_result()
    empty = CheckResult(verdict=result.verdict, academic=AcademicEvidence(supporting=0, refuting=0), mfds=result.mfds)
    card = render_reason(empty, image_base_url="https://example.com")["template"]["outputs"][0]["basicCard"]
    assert "buttons" not in card


# ── 관련 논문 판단 ───────────────────────────────────────────


def test_초록에만_효능이_있는_논문은_근거가_아니다():
    """실측: 혈관 기능 논문이 초록의 면역 언급으로 면역력 주장의 뒷받침이 됐다."""
    title = "Rg3-enriched Korean Red Ginseng improves vascular function in spontaneously hypertensive rats"
    assert is_relevant(title, "immune response was also measured", "Korean red ginseng", "immune function") is False


@pytest.mark.parametrize(
    ("title", "effect", "expected"),
    [
        ("Korean red ginseng enhances antiviral immunity", "immune function", True),
        ("Garlic supplementation and hypertension", "blood pressure", True),
        ("Omega-3 fatty acids and blood pressure", "hypertension", True),
        ("[Blood fluidity and omega-3 fatty acids]", "blood circulation", True),
        ("Health Benefits of Curcumin", "joint health", False),
        ("Lutein and macular pigment", "eye health", True),
        ("Probiotics and the gut microbiota", "gut health", True),
        ("Antioxidant effect of lutein in erythrocytes", "dementia", False),
    ],
)
def test_제목에서_같은_뜻의_표기까지_찾는다(title, effect, expected):
    assert effect_in_title(title, effect) is expected


@pytest.mark.parametrize(
    ("source", "title"),
    [("gemini", "영상 속 이 내용을 확인했어요"), ("title_only", "영상 속 이 내용을 확인했어요"),
     ("article", "글 속 이 내용을 확인했어요"), (None, "확인한 내용")],
)
def test_영상이나_글_전체가_아니라_그_안의_문장을_판정했다고_밝힌다(extractor, source, title):
    """실측: '무릎 관절 건강' 영상에서 칼슘·뼈 건강 문장을 판정해 믿을 만해요가 나갔다."""
    from healthcheck.domain.types import ExtractedContent

    claims = extractor.extract("칼슘이 뼈 건강에 도움이 됩니다")
    result = CheckResult(
        verdict=Verdict(label=Label.TRUSTWORTHY, rule_id="R-040", rules_version="t", reasons=()),
        claims=claims,
        primary_claim=claims.claims[0],
        content=ExtractedContent(text="x", has_transcript=True, confidence=0.75, source=source),
    )
    card = render(result, image_base_url="https://example.com")["template"]["outputs"][0]["basicCard"]
    assert card["title"] == title
    text_card = render(result)["template"]["outputs"][0]["textCard"]
    assert text_card["description"].startswith(title)
