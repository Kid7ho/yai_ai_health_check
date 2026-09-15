"""식약처 조회가 실패해도 논문 근거로 경고 쪽 판정을 낸다(R-090·R-091).

실측: 식약처 API 낮 시간 제한으로 블루베리 조회가 실패하자, 논문 결과가 있어도
'잠시 뒤에 다시 보내 주세요'만 나갔다.
"""

from __future__ import annotations

import pytest

from healthcheck.api.app import build_fake_orchestrator
from healthcheck.domain.labels import Label
from healthcheck.domain.types import Paper, VerdictInput
from healthcheck.pipeline import step1_input
from healthcheck.pipeline.step5_verdict import load_ruleset
from healthcheck.pipeline.step6_response import render_reason


def vi(**overrides) -> VerdictInput:
    base = dict(in_scope=True, transcript_quality="ok", disease_cure_expression=False, ad_violation_hit=False,
                recognition="UNKNOWN", functionality_match="NOT_APPLICABLE", supporting_papers=0, refuting_papers=0)
    base.update(overrides)
    return VerdictInput(**base)


@pytest.mark.parametrize(
    ("supporting", "refuting", "label", "rule"),
    [(1, 3, Label.WARNING, "R-090"), (2, 0, Label.CAUTION, "R-091"), (2, 1, Label.CAUTION, "R-091"), (0, 0, Label.UNKNOWN, "R-999")],
)
def test_식약처_조회_실패시_논문으로_경고_쪽만_판정한다(supporting, refuting, label, rule):
    verdict = load_ruleset().evaluate(vi(supporting_papers=supporting, refuting_papers=refuting))
    assert (verdict.label, verdict.rule_id) == (label, rule)


def test_식약처_조회_실패로는_믿을_만해요가_나오지_않는다():
    ruleset = load_ruleset()
    for supporting in range(0, 12):
        for refuting in range(0, 4):
            verdict = ruleset.evaluate(vi(supporting_papers=supporting, refuting_papers=refuting))
            assert verdict.label is not Label.TRUSTWORTHY


class FailingMfds:
    async def lookup(self, claim):
        raise RuntimeError("식약처 오류 ERROR-503: 09시~19시에는 서비스가 제한됩니다.")


class Papers:
    def __init__(self, stances):
        self.stances = stances

    async def search(self, claim, **kwargs):
        return tuple(Paper(title=f"Garlic and blood pressure {i}", url=f"https://p/{i}", stance=s) for i, s in enumerate(self.stances))


async def run(stances):
    orchestrator = build_fake_orchestrator()
    orchestrator.mfds = FailingMfds()
    orchestrator.academic = Papers(stances)
    text = "마늘이 혈압을 낮추는 데 좋다고 합니다"
    result = await orchestrator.run(step1_input.classify(text))
    cached = await orchestrator.cached(step1_input.classify(text))
    return result, cached


async def test_논문_근거가_있으면_다시_보내_달라고_하지_않고_판정을_보여_준다():
    result, cached = await run(["SUPPORT", "SUPPORT"])
    assert (result.verdict.label, result.verdict.rule_id) == (Label.CAUTION, "R-091")
    assert result.retryable_failure is False
    # 식약처를 확인하지 못한 판정은 굳히지 않는다.
    assert cached is None
    text = render_reason(result)["template"]["outputs"][0]["simpleText"]["text"]
    assert "지금 확인하지 못했어요" in text


async def test_논문_근거도_없으면_지금처럼_다시_보내_달라고_한다():
    result, cached = await run([])
    assert result.verdict.rule_id == "R-999"
    assert result.retryable_failure is True
    assert cached is None
