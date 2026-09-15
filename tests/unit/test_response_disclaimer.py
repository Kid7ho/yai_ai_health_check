"""⑥ 응답 생성 단위 테스트.

가장 중요한 검증은 면책 문구가 어떤 경로로도 빠지지 않는다는 것이다.
이 서비스는 진단하지 않는다. 결과는 근거 제시다.
"""

from __future__ import annotations

import inspect

import pytest

from healthcheck.domain.labels import LABEL_TEXT, Label
from healthcheck.domain.types import (
    AcademicEvidence,
    CheckResult,
    Claim,
    ClaimSet,
    MfdsEvidence,
    Paper,
    Verdict,
)
from healthcheck.pipeline import step6_response
from healthcheck.pipeline.step6_response import (
    DISCLAIMER,
    ack_response,
    find_forbidden,
    render,
    render_reason,
)


def result(label: Label, **overrides) -> CheckResult:
    verdict = Verdict(
        label=label,
        rule_id="R-TEST",
        rules_version="v0",
        reasons=("설명 문장", "근거 문장"),
    )
    base = dict(verdict=verdict)
    base.update(overrides)
    return CheckResult(**base)


def texts_of(payload: dict) -> list[str]:
    # 면책 문구가 카드 안에 붙으므로 카드 설명도 함께 본다.
    return [
        output.get("simpleText", {}).get("text")
        or output.get("textCard", {}).get("description")
        or output.get("basicCard", {}).get("description", "")
        for output in payload["template"]["outputs"]
    ]


# ─────────────────────────────────────────────────────────────
# 면책 문구는 구조다
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label", list(Label))
def test_네_라벨_모두에_면책_문구가_들어간다(label):
    assert any(DISCLAIMER in t for t in texts_of(render(result(label))))


@pytest.mark.parametrize("label", list(Label))
def test_근거_화면에도_면책_문구가_들어간다(label):
    assert any(DISCLAIMER in t for t in texts_of(render_reason(result(label))))


def test_범위_밖_거절_응답에도_면책_문구가_들어간다():
    """거절도 응답이다. 예외 경로로 빠져나가지 못한다."""
    claims = ClaimSet(claims=(), in_scope=False, disease_cure_expression=False)
    assert any(DISCLAIMER in t for t in texts_of(render(result(Label.UNKNOWN, claims=claims))))


def test_면책_문구는_마지막_말풍선의_끝에_붙는다():
    """어르신이 결과를 본 다음 마지막에 읽게 한다. 말풍선을 따로 늘리지 않는다."""
    outputs = render(result(Label.WARNING))["template"]["outputs"]
    assert len(outputs) == 1
    assert outputs[-1]["textCard"]["description"].endswith(DISCLAIMER)


def test_면책_문구를_끄는_인자가_없다():
    """개발자가 빠뜨릴 수 있는 선택 항목으로 두지 않는다."""
    # 카드 머리 이미지 주소만 받는다. 면책 문구나 금지 표현 검사를 끄는 인자는 두지 않는다.
    for fn in (render, render_reason):
        params = inspect.signature(fn).parameters
        assert list(params) == ["result", "image_base_url"], f"{fn.__name__}에 추가 인자가 생겼습니다."
        assert params["image_base_url"].kind is inspect.Parameter.KEYWORD_ONLY


def test_응답을_만드는_공개_함수를_늘리지_않는다():
    """outputs를 직접 조립하는 우회 경로를 만들지 않는다.

    find_forbidden은 문구를 만들지 않고 검사만 하므로 우회 경로가 아니다.
    """
    assert set(step6_response.__all__) == {
        "ack_response",
        "render",
        "render_reason",
        "find_forbidden",
        "DISCLAIMER",
        "Label",
        # 이미지 주소를 만드는 도구다. outputs를 조립하지 않는다.
        "verdict_image_url",
        "IMAGE_FILES",
        "IMAGE_VERSION",
    }


# ─────────────────────────────────────────────────────────────
# 카카오 포맷 제약
# ─────────────────────────────────────────────────────────────


def test_즉시_응답은_template_없이_useCallback만_담는다():
    """5초 제한을 지키는 기본 경로다."""
    ack = ack_response()
    assert ack["version"] == "2.0"
    assert ack["useCallback"] is True
    assert "template" not in ack


def test_카드_제목과_본문이_카카오_제한을_넘지_않는다():
    long_claim = Claim(
        ingredient="블루베리" * 30,
        claimed_effect="당뇨에 좋다" * 60,
        intensity="ASSERTIVE",
        span=(0, 1),
    )
    claims = ClaimSet(claims=(long_claim,), in_scope=True, disease_cure_expression=True)
    card = render(result(Label.WARNING, claims=claims))["template"]["outputs"][0]["textCard"]
    assert len(card["title"]) <= 50
    assert len(card["description"]) <= 400


def test_버튼은_세로_배치로_라벨_14자를_지킨다():
    """가로 2개 배치는 라벨 8자 제한이라 '왜 그런지 보기'가 들어가지 않는다."""
    card = render(result(Label.TRUSTWORTHY))["template"]["outputs"][0]["textCard"]
    assert card["buttonLayout"] == "vertical"
    assert len(card["buttons"]) <= 3
    for button in card["buttons"]:
        assert len(button["label"]) <= 14


def test_결과_공유_버튼은_share_액션을_쓴다():
    """발표 자료의 '가족에게 보내기' 동선이다."""
    buttons = render(result(Label.CAUTION))["template"]["outputs"][0]["textCard"]["buttons"]
    assert any(button["action"] == "share" for button in buttons)


def test_말풍선_길이가_1000자를_넘지_않는다():
    papers = tuple(
        Paper(title="논문 제목" * 80, url=f"https://example.com/{i}") for i in range(3)
    )
    payload = render_reason(
        result(
            Label.CAUTION,
            academic=AcademicEvidence(supporting=3, refuting=0, papers=papers),
        )
    )
    for text in texts_of(payload):
        assert len(text) <= 1000


# ─────────────────────────────────────────────────────────────
# 카드 내용
# ─────────────────────────────────────────────────────────────


def test_카드_제목은_어르신이_읽는_한국어_문구다():
    card = render(result(Label.WARNING))["template"]["outputs"][0]["textCard"]
    assert card["title"] == LABEL_TEXT[Label.WARNING] == "주의하세요"


def test_확인한_내용에_성분과_주장이_들어간다():
    claim = Claim(
        ingredient="블루베리",
        claimed_effect="당뇨를 완치시킨다",
        intensity="ASSERTIVE",
        span=(0, 10),
    )
    claims = ClaimSet(claims=(claim,), in_scope=True, disease_cure_expression=True)
    card = render(result(Label.WARNING, claims=claims))["template"]["outputs"][0]["textCard"]
    assert "블루베리" in card["description"]
    assert "당뇨를 완치시킨다" in card["description"]


def test_근거_화면에_식약처_출처가_표시된다():
    mfds = MfdsEvidence(
        recognition="NOTIFIED",
        functionality_text="면역력 증진에 도움을 줄 수 있음",
        match_level="EXACT",
    )
    text = "\n".join(texts_of(render_reason(result(Label.TRUSTWORTHY, mfds=mfds))))
    assert "식품의약품안전처" in text
    assert "면역력 증진" in text


def test_조회_실패한_출처는_근거로_쓰지_않는다():
    """실패한 조회를 근거처럼 보여 주면 안 된다."""
    mfds = MfdsEvidence(
        recognition="INDIVIDUAL",
        functionality_text="혈당 조절",
        match_level="EXACT",
        lookup_failed=True,
    )
    text = "\n".join(texts_of(render_reason(result(Label.UNKNOWN, mfds=mfds))))
    assert "혈당 조절" not in text


def test_범위_밖이면_판정_카드_대신_거절_안내를_준다():
    claims = ClaimSet(claims=(), in_scope=False, disease_cure_expression=False)
    outputs = render(result(Label.UNKNOWN, claims=claims))["template"]["outputs"]
    assert all("textCard" not in output for output in outputs)
    assert "의사" in outputs[0]["simpleText"]["text"]


def test_일시적_실패는_알_수_없어요로_답하지_않는다():
    """다시 보내면 달라질 수 있는 상태다. 판정이 끝난 것처럼 보이면 안 된다."""
    payload = render(CheckResult(verdict=result(Label.UNKNOWN).verdict, retryable_failure=True))
    texts = texts_of(payload)

    assert "잠시 뒤에 다시 보내" in texts[0]
    assert any(DISCLAIMER in t for t in texts)
    assert all("textCard" not in output for output in payload["template"]["outputs"])


# ─────────────────────────────────────────────────────────────
# 진단처럼 읽히는 문구 차단
#
# 이 서비스의 핵심은 진단하지 않는다는 점이다. 면책 문구가 붙는지는 위에서
# 검사하지만, 카드 본문 자체가 진단처럼 읽히면 면책 문구만으로는 부족하다.
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phrase",
    [
        "진단 결과 당뇨가 의심됩니다",
        "이 제품을 복용하세요",
        "약 복용을 중단하세요",
        "꾸준히 드시면 낫습니다",
        "이것으로 치료됩니다",
        "병원에 가지 않아도 됩니다",
        "처방 없이 드실 수 있습니다",
    ],
)
def test_진단처럼_읽히는_표현을_찾아낸다(phrase):
    assert find_forbidden(phrase)


def test_면책_문구는_예외다():
    """면책 문구에는 '진단'이 들어간다. 이것을 금지로 잡으면 안 된다."""
    assert find_forbidden(DISCLAIMER) == []


def test_범위_밖_안내도_예외다():
    """'약이나 치료에 관한 것은 의사 선생님께'는 오히려 권장 문구다."""
    claims = ClaimSet(claims=(), in_scope=False, disease_cure_expression=False)
    payload = render(result(Label.UNKNOWN, claims=claims))

    assert "의사" in texts_of(payload)[0]
    assert any(DISCLAIMER in t for t in texts_of(payload))


@pytest.mark.parametrize("label", list(Label))
def test_네_라벨의_실제_응답에_금지_표현이_없다(label):
    """규칙표 v0.2의 근거 문장이 그대로 어르신에게 나간다."""
    for payload in (render(result(label)), render_reason(result(label))):
        for text in texts_of(payload):
            assert find_forbidden(text) == [], text


def test_규칙표_전체_근거_문장에_금지_표현이_없다():
    """자문의가 규칙표를 고칠 때 진단 표현을 쓰면 여기서 잡힌다."""
    from healthcheck.pipeline.step5_verdict import load_ruleset

    for rule in load_ruleset().rules:
        assert find_forbidden(rule.description) == [], rule.id
        assert find_forbidden(rule.rationale) == [], rule.id


def test_금지_표현이_섞이면_문구를_바꿔_내보낸다():
    """조용히 통과시키지 않는다. 근거를 못 보여 주더라도 진단처럼 말하지 않는다."""
    verdict = Verdict(
        label=Label.WARNING,
        rule_id="R-TEST",
        rules_version="v0.2",
        reasons=("설명", "꾸준히 드시면 낫습니다"),
    )
    # 판정 이유는 근거 화면에 나간다. 이미지 카드와 말풍선 두 경로를 모두 본다.
    card = render_reason(CheckResult(verdict=verdict), image_base_url="https://example.com")[
        "template"
    ]["outputs"][0]["basicCard"]
    assert "낫습니다" not in card["description"]
    assert card["description"] == f"확인한 근거를 보여 드리기 어려워요.\n\n{DISCLAIMER}"

    text = render_reason(CheckResult(verdict=verdict))["template"]["outputs"][0]["simpleText"]["text"]
    assert "낫습니다" not in text
    assert text == f"확인한 근거를 보여 드리기 어려워요.\n\n{DISCLAIMER}"


def test_라벨_문구_자체에_금지_표현이_없다():
    for text in LABEL_TEXT.values():
        assert find_forbidden(text) == []


def test_다른_효능으로_인정받은_사실을_구분해_설명한다():
    """성분은 인정 원료인데 주장 효능만 미인정인 경우다.

    이 구분을 알려 주지 않으면 어르신이 "인정 안 된 성분"으로 오해한다.
    """
    mfds = MfdsEvidence(
        recognition="NOT_RECOGNIZED",
        functionality_text=None,
        match_level="NOT_APPLICABLE",
        other_recognitions=("간 건강에 도움을 줄 수 있음",),
    )
    text = "\n".join(texts_of(render_reason(result(Label.CAUTION, mfds=mfds))))

    assert "다른 기능성으로는 인정받았습니다" in text
    assert "간 건강" in text
    assert find_forbidden(text) == []
