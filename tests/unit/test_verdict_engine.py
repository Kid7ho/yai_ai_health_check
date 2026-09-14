"""규칙 엔진 단위 테스트.

외부 API를 전혀 쓰지 않고 판정 규칙 엔진을 완전히 검증한다.
규칙표 자체의 오류를 로딩 단계에서 잡는지도 함께 확인한다.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from healthcheck.domain.labels import Label
from healthcheck.domain.types import (
    AcademicEvidence,
    ClaimSet,
    ExtractedContent,
    MfdsEvidence,
    VerdictInput,
)
from healthcheck.pipeline.step5_verdict import (
    RuleConfigError,
    build_verdict_input,
    load_ruleset,
)


def vi(**overrides) -> VerdictInput:
    """판정 입력 기본값. 테스트마다 필요한 항목만 바꿔 쓴다."""
    base = dict(
        in_scope=True,
        transcript_quality="ok",
        disease_cure_expression=False,
        ad_violation_hit=False,
        prohibited_ad_expression=False,
        recognition="NOT_RECOGNIZED",
        functionality_match="NOT_APPLICABLE",
        supporting_papers=0,
        refuting_papers=0,
    )
    base.update(overrides)
    return VerdictInput(**base)


def write_rules(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "rules.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────
# 규칙표 v0의 판정 동작
# ─────────────────────────────────────────────────────────────


def test_범위_밖_내용은_판정하지_않는다():
    verdict = load_ruleset().evaluate(vi(in_scope=False))
    assert verdict.label is Label.UNKNOWN
    assert verdict.rule_id == "R-001"


@pytest.mark.parametrize("quality", ["none", "low"])
def test_자막이_없거나_부실하면_판정을_보류한다(quality):
    verdict = load_ruleset().evaluate(vi(transcript_quality=quality))
    assert verdict.label is Label.UNKNOWN
    assert verdict.rule_id == "R-010"


def test_질병_치료_표현은_인정_원료라도_주의로_간다():
    """국내법상 질병을 낫게 한다고 광고할 수 있는 식품은 없다.

    인정 원료이고 효능이 완전히 일치하고 논문이 뒷받침해도
    질병 치료 표현이 있으면 TRUSTWORTHY로 갈 수 없다.
    """
    verdict = load_ruleset().evaluate(
        vi(
            disease_cure_expression=True,
            recognition="INDIVIDUAL",
            functionality_match="EXACT",
            supporting_papers=10,
            refuting_papers=0,
        )
    )
    assert verdict.label is Label.WARNING
    assert verdict.rule_id == "R-021"


def test_질병_표현과_부당광고_이력이_겹치면_전용_규칙이_잡는다():
    verdict = load_ruleset().evaluate(vi(disease_cure_expression=True, ad_violation_hit=True))
    assert verdict.rule_id == "R-020"
    assert verdict.label is Label.WARNING


def test_부당광고_적발_이력만_있어도_주의로_간다():
    verdict = load_ruleset().evaluate(vi(ad_violation_hit=True, recognition="INDIVIDUAL"))
    assert verdict.label is Label.WARNING
    assert verdict.rule_id == "R-030"


@pytest.mark.parametrize("recognition", ["INDIVIDUAL", "NOTIFIED"])
def test_인정_원료에_효능_일치하고_반박_없으면_신뢰(recognition):
    verdict = load_ruleset().evaluate(
        vi(recognition=recognition, functionality_match="EXACT", supporting_papers=1)
    )
    assert verdict.label is Label.TRUSTWORTHY
    assert verdict.rule_id == "R-040"


def test_뒷받침_논문이_없으면_신뢰를_주지_않는다():
    """신뢰_최소_뒷받침논문 임계값이 실제로 걸리는지 확인한다."""
    verdict = load_ruleset().evaluate(
        vi(recognition="INDIVIDUAL", functionality_match="EXACT", supporting_papers=0)
    )
    assert verdict.label is Label.CAUTION
    # 논문이 없는 경우는 반박 논문이 있는 R-041과 구분해 R-042가 받는다.
    assert verdict.rule_id == "R-042"


def test_반박_논문이_적으면_신뢰를_준다():
    """실제 학술 검색은 어떤 주장이든 반박 논문을 한두 편 돌려준다.

    반박 0편을 요구하면 국가 인정 문구와 정확히 일치하는 주장까지
    TRUSTWORTHY에 도달하지 못한다. 오메가3·혈액순환이 4편 대 2편,
    강황·관절이 6편 대 2편이었는데 둘 다 CAUTION으로 떨어졌다.
    """
    verdict = load_ruleset().evaluate(
        vi(
            recognition="INDIVIDUAL",
            functionality_match="EXACT",
            supporting_papers=4,
            refuting_papers=2,
        )
    )
    assert verdict.label is Label.TRUSTWORTHY
    assert verdict.rule_id == "R-040"


def test_반박_논문이_많으면_단정하지_않는다():
    """뒷받침이 반박의 두 배에 못 미치면 신뢰를 주지 않는다."""
    verdict = load_ruleset().evaluate(
        vi(
            recognition="INDIVIDUAL",
            functionality_match="EXACT",
            supporting_papers=3,
            refuting_papers=2,
        )
    )
    assert verdict.label is Label.CAUTION
    assert verdict.rule_id == "R-041"


def test_인정_범위보다_부풀린_주장은_조심():
    verdict = load_ruleset().evaluate(vi(recognition="INDIVIDUAL", functionality_match="PARTIAL"))
    assert verdict.label is Label.CAUTION
    assert verdict.rule_id == "R-050"


def test_인정_사실을_엉뚱한_효능에_붙이면_주의():
    verdict = load_ruleset().evaluate(vi(recognition="INDIVIDUAL", functionality_match="MISMATCH"))
    assert verdict.label is Label.WARNING
    assert verdict.rule_id == "R-060"


def test_미인정_원료에_연구만_있으면_조심():
    verdict = load_ruleset().evaluate(vi(recognition="NOT_RECOGNIZED", supporting_papers=2))
    assert verdict.label is Label.CAUTION
    assert verdict.rule_id == "R-070"


def test_반박이_섞여도_뒷받침이_다수면_조심():
    """실제 학술 검색에서는 어떤 주장이든 반박 논문이 한두 편 나온다.

    반박 0편을 요구하는 R-070만으로는 국가 인정이 없는 성분이 거의 전부
    UNKNOWN으로 떨어진다. Liner 실제 호출에서 마늘·혈압이 뒷받침 7편
    반박 2편이었고 판정이 UNKNOWN이었다.
    """
    verdict = load_ruleset().evaluate(
        vi(recognition="NOT_RECOGNIZED", supporting_papers=7, refuting_papers=2)
    )
    assert verdict.label is Label.CAUTION
    assert verdict.rule_id == "R-071"


def test_뒷받침이_다수_기준에_못_미치면_판정을_보류한다():
    """임계값 다수연구_최소_뒷받침논문이 실제로 걸리는지 확인한다."""
    verdict = load_ruleset().evaluate(
        vi(recognition="NOT_RECOGNIZED", supporting_papers=1, refuting_papers=1)
    )
    assert verdict.rule_id == "R-999"


def test_반박_논문이_우세하면_주의():
    """파생 조건 '반박논문_우세'가 임계값 2로 계산되는지 확인한다."""
    ruleset = load_ruleset()

    우세 = ruleset.evaluate(vi(supporting_papers=1, refuting_papers=3))
    assert 우세.rule_id == "R-080"
    assert 우세.label is Label.WARNING

    # 차이가 1이면 아직 우세가 아니다. 기본값으로 떨어진다.
    미달 = ruleset.evaluate(vi(supporting_papers=1, refuting_papers=2))
    assert 미달.rule_id == "R-999"


def test_어디에도_맞지_않으면_알_수_없어요():
    """보수적 기본값. 오판보다 판정 보류가 안전하다."""
    verdict = load_ruleset().evaluate(vi(recognition="UNKNOWN"))
    assert verdict.label is Label.UNKNOWN
    assert verdict.rule_id == "R-999"


def test_판정_결과에_역추적_정보가_담긴다():
    verdict = load_ruleset().evaluate(vi(in_scope=False))
    assert verdict.rules_version == "v0.9"
    assert verdict.rule_id
    assert verdict.reasons and all(verdict.reasons)
    assert verdict.inputs is not None


def test_규칙표_v0에_네_라벨이_모두_등장한다():
    """쓰이지 않는 라벨이 있으면 규칙표가 불완전하다는 신호다."""
    labels = {rule.label for rule in load_ruleset().rules}
    assert labels == set(Label)


# ─────────────────────────────────────────────────────────────
# 규칙표 검증 — 잘못된 규칙표로는 기동하지 못한다
# ─────────────────────────────────────────────────────────────

# 아래 규칙표 조각들은 테스트 본문의 들여쓰기(8칸)에 맞춰 둔다.
# textwrap.dedent가 본문과 이 조각을 함께 처리하기 때문이다.
MINIMAL_TAIL = """
          - id: R-999
            설명: 기본값
            조건: {}
            결과: UNKNOWN
            근거: 보수적 기본값
"""


def test_기본_규칙이_없으면_로딩을_거부한다(tmp_path):
    path = write_rules(
        tmp_path,
        """
        version: test
        규칙:
          - id: R-001
            설명: 조건이 있는 마지막 규칙
            조건:
              in_scope: false
            결과: UNKNOWN
            근거: 기본 규칙이 없다
        """,
    )
    with pytest.raises(RuleConfigError, match="기본 규칙"):
        load_ruleset(path)


def test_기본값이_UNKNOWN이_아니면_로딩을_거부한다(tmp_path):
    path = write_rules(
        tmp_path,
        """
        version: test
        규칙:
          - id: R-999
            설명: 위험한 기본값
            조건: {}
            결과: TRUSTWORTHY
            근거: 이런 규칙표는 거부되어야 한다
        """,
    )
    with pytest.raises(RuleConfigError, match="UNKNOWN"):
        load_ruleset(path)


def test_조건_없는_규칙이_중간에_있으면_거부한다(tmp_path):
    """뒤 규칙이 영원히 평가되지 않는 규칙표를 막는다."""
    path = write_rules(
        tmp_path,
        """
        version: test
        규칙:
          - id: R-100
            설명: 조건이 없어 항상 일치한다
            조건: {}
            결과: CAUTION
            근거: 뒤 규칙을 죽인다
        """
        + MINIMAL_TAIL,
    )
    with pytest.raises(RuleConfigError, match="평가되지 않습니다"):
        load_ruleset(path)


def test_오타_난_조건_이름을_거부한다(tmp_path):
    """조건 이름이 조용히 무시되면 규칙 하나가 통째로 동작하지 않는다."""
    path = write_rules(
        tmp_path,
        """
        version: test
        규칙:
          - id: R-100
            설명: 조건 이름에 오타가 있다
            조건:
              disease_cure_expresion: true
            결과: WARNING
            근거: 오타
        """
        + MINIMAL_TAIL,
    )
    with pytest.raises(RuleConfigError, match="판정 입력에 없습니다"):
        load_ruleset(path)


def test_없는_라벨을_거부한다(tmp_path):
    path = write_rules(
        tmp_path,
        """
        version: test
        규칙:
          - id: R-100
            설명: 없는 라벨
            조건:
              in_scope: false
            결과: DANGEROUS
            근거: 라벨은 네 개뿐이다
        """
        + MINIMAL_TAIL,
    )
    with pytest.raises(RuleConfigError, match="없는 라벨"):
        load_ruleset(path)


def test_규칙_ID_중복을_거부한다(tmp_path):
    path = write_rules(
        tmp_path,
        """
        version: test
        규칙:
          - id: R-100
            설명: 첫 번째
            조건:
              in_scope: false
            결과: UNKNOWN
            근거: 역추적을 위해 ID는 유일해야 한다
          - id: R-100
            설명: 같은 ID
            조건:
              ad_violation_hit: true
            결과: WARNING
            근거: 중복
        """
        + MINIMAL_TAIL,
    )
    with pytest.raises(RuleConfigError, match="중복"):
        load_ruleset(path)


def test_version이_없으면_거부한다(tmp_path):
    """판정 로그에 규칙 버전을 남길 수 없으면 역추적이 불가능하다."""
    path = write_rules(tmp_path, "규칙:" + MINIMAL_TAIL)
    with pytest.raises(RuleConfigError, match="version"):
        load_ruleset(path)


def test_정의되지_않은_임계값_이름을_거부한다(tmp_path):
    path = write_rules(
        tmp_path,
        """
        version: test
        임계값:
          존재하는_기준: 1
        규칙:
          - id: R-100
            설명: 없는 임계값을 참조한다
            조건:
              supporting_papers:
                최소: 없는_기준
            결과: TRUSTWORTHY
            근거: 임계값 오타
        """
        + MINIMAL_TAIL,
    )
    # 평가 시점이 아니라 로딩 시점에 잡아야 한다.
    with pytest.raises(RuleConfigError, match="임계값"):
        load_ruleset(path)


# ─────────────────────────────────────────────────────────────
# 판정 입력 만들기
# ─────────────────────────────────────────────────────────────

THRESHOLDS = {"자막_신뢰도_최저": 0.6, "반박논문_우세_기준": 2, "신뢰_최소_뒷받침논문": 1}


def claims(**overrides) -> ClaimSet:
    base = dict(claims=(), in_scope=True, disease_cure_expression=False)
    base.update(overrides)
    return ClaimSet(**base)


def test_자막_신뢰도가_임계값보다_낮으면_low():
    built = build_verdict_input(
        claims=claims(),
        content=ExtractedContent(text="내용", has_transcript=True, confidence=0.3),
        academic=None,
        mfds=None,
        thresholds=THRESHOLDS,
    )
    assert built.transcript_quality == "low"


def test_자막이_없으면_none():
    built = build_verdict_input(
        claims=claims(),
        content=ExtractedContent(text="", has_transcript=False, confidence=0.0),
        academic=None,
        mfds=None,
        thresholds=THRESHOLDS,
    )
    assert built.transcript_quality == "none"


def test_외부_조회_실패를_낙관적으로_채우지_않는다():
    """조회 실패가 TRUSTWORTHY로 흐르는 경로를 만들지 않는다."""
    built = build_verdict_input(
        claims=claims(),
        content=ExtractedContent(text="홍삼이 면역력에 좋다", has_transcript=True, confidence=0.9),
        academic=AcademicEvidence(supporting=99, refuting=0, lookup_failed=True),
        mfds=MfdsEvidence(
            recognition="INDIVIDUAL",
            functionality_text="면역력 증진",
            match_level="EXACT",
            lookup_failed=True,
        ),
        thresholds=THRESHOLDS,
    )
    assert built.recognition == "UNKNOWN"
    assert built.functionality_match == "NOT_APPLICABLE"
    assert built.supporting_papers == 0
    assert load_ruleset().evaluate(built).label is Label.UNKNOWN


def test_법으로_금지된_광고_표현은_주의로_간다():
    """식약처 적발 기록 조회가 아니라 표현 매칭 결과로 판정한다."""
    verdict = load_ruleset().evaluate(
        vi(
            prohibited_ad_expression=True,
            recognition="INDIVIDUAL",
            functionality_match="EXACT",
            supporting_papers=10,
        )
    )
    assert verdict.label is Label.WARNING
    assert verdict.rule_id == "R-031"


def test_적발_이력_규칙은_여전히_데이터가_없어_발동하지_않는다():
    """ad_violation_hit은 전용 API를 확보할 때까지 항상 False다.

    규칙 R-020과 R-030 자체는 살아 있다. 데이터가 들어오면 바로 동작한다.
    """
    ruleset = load_ruleset()

    assert ruleset.evaluate(vi(ad_violation_hit=True)).rule_id == "R-030"
    assert (
        ruleset.evaluate(vi(ad_violation_hit=True, disease_cure_expression=True)).rule_id
        == "R-020"
    )
