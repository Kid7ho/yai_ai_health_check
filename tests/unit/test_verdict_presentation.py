"""⑥ 판정 점검에서 찾은 결함 테스트.

  · 결과 카드에 검수용 근거 문장이 그대로 나갔다
  · 논문을 찾지 못한 경우에도 "학계에 이견이 있다"고 말했다
  · 근거 조회가 실패해도 내용 판단처럼 '조심하세요'로 답했다
"""

from __future__ import annotations

import pytest

from healthcheck.domain.labels import Label
from healthcheck.domain.types import CheckResult, VerdictInput
from healthcheck.pipeline.step5_verdict import load_ruleset
from healthcheck.pipeline.step6_response import find_forbidden, render, render_reason


def vi(**overrides) -> VerdictInput:
    base = dict(
        in_scope=True, transcript_quality="ok", disease_cure_expression=False, ad_violation_hit=False,
        recognition="NOT_RECOGNIZED", functionality_match="NOT_APPLICABLE", supporting_papers=0, refuting_papers=0,
    )
    base.update(overrides)
    return VerdictInput(**base)


def texts(payload) -> list[str]:
    outs = payload["template"]["outputs"]
    return [o.get("textCard", {}).get("description") or o.get("simpleText", {}).get("text", "") for o in outs]


# ── 어르신용 안내 ────────────────────────────────────────────


@pytest.mark.parametrize("rule", load_ruleset().rules, ids=lambda r: r.id)
def test_모든_규칙에_짧은_안내가_있다(rule):
    assert rule.guidance, f"{rule.id}에 안내가 없습니다"
    assert len(rule.guidance) <= 100
    assert find_forbidden(rule.guidance) == []


def test_카드에는_검수용_근거_대신_안내가_나간다():
    """이전에는 '반박 논문이 한 편도 없어야…' 같은 검수용 설명이 카드에 나갔다."""
    verdict = load_ruleset().evaluate(
        vi(recognition="NOTIFIED", functionality_match="EXACT", supporting_papers=4, refuting_papers=1)
    )
    description = texts(render(CheckResult(verdict=verdict)))[0]

    assert "나라가 인정한 기능성과 같고" in description
    assert "반박 논문이 한 편도" not in description


def test_근거_화면도_검수용_근거를_보여_주지_않는다():
    verdict = load_ruleset().evaluate(vi())
    text = "\n".join(texts(render_reason(CheckResult(verdict=verdict))))

    assert "판단을 미룰게요" in text
    assert "보수적 기본값" not in text


# ── 논문을 못 찾은 경우 ──────────────────────────────────────


def test_논문이_없으면_이견이_아니라_연구를_못_찾았다고_말한다():
    verdict = load_ruleset().evaluate(vi(recognition="NOTIFIED", functionality_match="EXACT"))

    assert verdict.rule_id == "R-042"
    assert verdict.label is Label.CAUTION
    assert "찾지 못했어요" in verdict.guidance


def test_반박_논문이_있으면_엇갈린다고_말한다():
    verdict = load_ruleset().evaluate(
        vi(recognition="NOTIFIED", functionality_match="EXACT", supporting_papers=3, refuting_papers=2)
    )
    assert verdict.rule_id == "R-041"
    assert "엇갈려요" in verdict.guidance


# ── 근거 조회 실패 ───────────────────────────────────────────


def test_근거를_쓰는_규칙을_구분한다():
    ruleset = load_ruleset()
    assert ruleset.uses_evidence("R-040") is True
    assert ruleset.uses_evidence("R-999") is True
    assert ruleset.uses_evidence("R-021") is False
    assert ruleset.uses_evidence("R-031") is False
    assert ruleset.uses_evidence("ERR-PIPELINE") is False


class BrokenAcademic:
    async def search(self, claim, **kwargs):
        raise RuntimeError("Liner 일시 장애")


async def test_학술_조회가_실패하면_조심하세요_대신_다시_보내_달라고_한다():
    """이전에는 조회 실패를 '학계에 이견이 있다'로 설명했다."""
    from healthcheck.api.app import build_fake_orchestrator
    from healthcheck.pipeline import step1_input

    orchestrator = build_fake_orchestrator()
    orchestrator.academic = BrokenAcademic()
    result = await orchestrator.run(step1_input.classify("홍삼이 면역력 증진에 도움을 줄 수 있다"))

    assert result.retryable_failure is True
    assert "잠시 뒤에 다시" in texts(render(result))[0]


async def test_근거와_무관한_질병_치료_판정은_조회가_실패해도_보여_준다():
    from healthcheck.api.app import build_fake_orchestrator
    from healthcheck.pipeline import step1_input

    orchestrator = build_fake_orchestrator()
    orchestrator.academic = BrokenAcademic()
    result = await orchestrator.run(step1_input.classify("홍삼을 먹으면 당뇨가 완치됩니다"))

    assert result.verdict.label is Label.WARNING
    assert result.retryable_failure is False
