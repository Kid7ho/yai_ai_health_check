"""⑤ 판정 — 규칙 엔진.

규칙표(rules/verdict_rules_v0.yaml)를 읽어 위에서 아래 순서로 평가하고
처음 일치한 규칙의 결과를 돌려준다. 판정 논리는 이 파일에 없다.
이 파일은 규칙표를 해석해 실행하는 기계일 뿐이다.

설계 원칙 두 가지를 코드로 강제한다.

1. 보수적 기본값
   규칙표의 마지막 규칙은 조건이 비어 있고 결과가 UNKNOWN이어야 한다.
   그렇지 않은 규칙표는 로딩 단계에서 거부한다. 어느 규칙에도 맞지 않는
   입력이 라벨 없이 빠져나가는 경로를 만들지 않기 위해서다.

2. 역추적 가능성
   판정 결과에는 사용된 규칙 버전과 매칭된 규칙 ID, 그리고 판정에 쓰인
   입력값 전체가 함께 담긴다. 어떤 판정이 왜 나왔는지 나중에 되짚을 수 있다.

외부 의존성이 전혀 없다. 단위 테스트만으로 완전히 검증할 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Callable

import yaml

from healthcheck.domain.labels import Label
from healthcheck.domain.types import (
    AcademicEvidence,
    ClaimSet,
    ExtractedContent,
    MfdsEvidence,
    Verdict,
    VerdictInput,
)


class RuleConfigError(ValueError):
    """규칙표 자체가 잘못된 경우. 서비스 기동 시점에 터뜨린다."""


# ─────────────────────────────────────────────────────────────
# 파생 값
# ─────────────────────────────────────────────────────────────
# VerdictInput에 그대로 담기지 않고, 임계값과 함께 계산해야 나오는 조건들이다.
# 규칙표에서 사람이 읽기 쉬운 이름으로 참조할 수 있게 여기에 등록한다.

DerivedFn = Callable[[VerdictInput, dict[str, Any]], bool]


def _refuting_dominant(vi: VerdictInput, thresholds: dict[str, Any]) -> bool:
    """반박 논문이 뒷받침 논문보다 기준만큼 많은 상태."""
    margin = thresholds["반박논문_우세_기준"]
    return (vi.refuting_papers - vi.supporting_papers) >= margin


def _supporting_dominant(vi: VerdictInput, thresholds: dict[str, Any]) -> bool:
    """뒷받침 논문이 반박 논문보다 분명히 많은 상태.

    반박 논문이 한 편도 없어야 한다는 조건은 현실에서 거의 성립하지 않는다.
    실제 학술 검색은 어떤 주장이든 반박 논문을 한두 편 돌려준다. 국가가
    인정한 기능성 문구와 정확히 일치하는 주장까지 '믿을 만해요'에 도달하지
    못하면 그 라벨이 사실상 존재하지 않게 된다. 그래서 배수로 본다.
    """
    multiple = thresholds["신뢰_뒷받침_배수"]
    minimum = thresholds["신뢰_최소_뒷받침논문"]
    if vi.supporting_papers < minimum:
        return False
    return vi.supporting_papers >= vi.refuting_papers * multiple


# 식약처·학술 근거에서 오는 판정 입력. 이 값을 쓰는 규칙은 조회 실패에 영향을 받는다.
EVIDENCE_CONDITIONS = frozenset(
    {"recognition", "functionality_match", "supporting_papers", "refuting_papers", "뒷받침_우세", "반박논문_우세"}
)

DERIVED_CONDITIONS: dict[str, DerivedFn] = {
    "반박논문_우세": _refuting_dominant,
    "뒷받침_우세": _supporting_dominant,
}

# VerdictInput이 실제로 가진 필드 이름. 규칙표 검증에 쓴다.
_INPUT_FIELDS = frozenset(f.name for f in fields(VerdictInput))


# ─────────────────────────────────────────────────────────────
# 규칙표 자료구조
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Rule:
    """규칙 한 건."""

    id: str
    description: str
    conditions: dict[str, Any]
    label: Label
    rationale: str
    # 어르신에게 보여 줄 짧은 안내. 검수용 rationale과 따로 둔다.
    guidance: str = ""
    # 식약처 조회가 실패한 상황을 위해 만든 규칙인가. 이 규칙의 판정은 조회 실패여도
    # '잠시 뒤에 다시'로 바꾸지 않고 그대로 보여 준다(캐시는 하지 않는다).
    usable_without_mfds: bool = False

    @property
    def uses_evidence(self) -> bool:
        """판정에 식약처·학술 근거가 쓰였는가.

        기본 규칙(조건 없음)도 근거가 부족해서 떨어진 것일 수 있으므로 포함한다.
        근거 조회가 실패했을 때 이 규칙으로 나온 판정은 믿을 수 없다.
        """
        if self.usable_without_mfds:
            return False
        if not self.conditions:
            return True
        return bool(set(self.conditions) & EVIDENCE_CONDITIONS)

    def matches(self, vi: VerdictInput, thresholds: dict[str, Any]) -> bool:
        """조건 전부를 만족해야 일치다. 조건이 비어 있으면 항상 일치한다."""
        return all(
            _check_condition(name, expected, vi, thresholds)
            for name, expected in self.conditions.items()
        )


@dataclass(frozen=True, slots=True)
class RuleSet:
    """규칙표 전체."""

    version: str
    thresholds: dict[str, Any]
    rules: tuple[Rule, ...]

    def uses_evidence(self, rule_id: str) -> bool:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule.uses_evidence
        return False

    def evaluate(self, vi: VerdictInput) -> Verdict:
        """위에서 아래로 평가하고 처음 일치한 규칙의 결과를 돌려준다."""
        for rule in self.rules:
            if rule.matches(vi, self.thresholds):
                return Verdict(
                    label=rule.label,
                    rule_id=rule.id,
                    rules_version=self.version,
                    reasons=(rule.description, rule.rationale),
                    inputs=vi,
                    guidance=rule.guidance,
                )
        # 마지막 규칙이 조건 없는 UNKNOWN임을 로딩 시점에 검증하므로
        # 여기까지 내려올 수 없다. 그래도 방어적으로 UNKNOWN을 돌려준다.
        raise RuleConfigError(
            "어느 규칙에도 일치하지 않았습니다. 규칙표에 기본 규칙이 없습니다."
        )


# ─────────────────────────────────────────────────────────────
# 조건 평가
# ─────────────────────────────────────────────────────────────


def _resolve_number(value: Any, thresholds: dict[str, Any], rule_hint: str) -> float:
    """숫자이거나, 임계값 절에 정의된 이름이다."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value not in thresholds:
            raise RuleConfigError(
                f"{rule_hint}: 임계값 '{value}'가 임계값 절에 정의되어 있지 않습니다."
            )
        return thresholds[value]
    raise RuleConfigError(f"{rule_hint}: 숫자 조건에 쓸 수 없는 값입니다: {value!r}")


def _check_condition(
    name: str,
    expected: Any,
    vi: VerdictInput,
    thresholds: dict[str, Any],
) -> bool:
    """조건 하나를 평가한다.

    조건 형태는 세 가지다.
      값 하나        recognition: NOT_RECOGNIZED
      값 목록        transcript_quality: [none, low]   (하나라도 맞으면 일치)
      숫자 범위      refuting_papers: {최대: 0}
    """
    if name in DERIVED_CONDITIONS:
        actual: Any = DERIVED_CONDITIONS[name](vi, thresholds)
    else:
        actual = getattr(vi, name)

    # 숫자 범위
    if isinstance(expected, dict):
        hint = f"조건 '{name}'"
        if "최소" in expected and actual < _resolve_number(expected["최소"], thresholds, hint):
            return False
        if "최대" in expected and actual > _resolve_number(expected["최대"], thresholds, hint):
            return False
        unknown = set(expected) - {"최소", "최대"}
        if unknown:
            raise RuleConfigError(f"{hint}: 알 수 없는 범위 키 {sorted(unknown)}")
        return True

    # 값 목록
    if isinstance(expected, list):
        return actual in expected

    # 값 하나
    return actual == expected


# ─────────────────────────────────────────────────────────────
# 규칙표 로딩과 검증
# ─────────────────────────────────────────────────────────────

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[3] / "rules" / "verdict_rules_v0.yaml"


def load_ruleset(path: Path | str = DEFAULT_RULES_PATH) -> RuleSet:
    """규칙표를 읽고 검증한다.

    검증에 실패하면 RuleConfigError를 던진다. 잘못된 규칙표로 서비스가
    기동해서 엉뚱한 판정을 내리는 것보다, 기동 자체를 막는 편이 안전하다.
    """
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    if not isinstance(raw, dict):
        raise RuleConfigError(f"{path}: 규칙표 최상위가 매핑이 아닙니다.")

    version = raw.get("version")
    if not isinstance(version, str) or not version:
        raise RuleConfigError(f"{path}: version이 없습니다. 판정 로그에 남길 값입니다.")

    thresholds = raw.get("임계값") or {}
    if not isinstance(thresholds, dict):
        raise RuleConfigError(f"{path}: 임계값 절이 매핑이 아닙니다.")

    raw_rules = raw.get("규칙")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RuleConfigError(f"{path}: 규칙이 하나도 없습니다.")

    rules: list[Rule] = []
    seen_ids: set[str] = set()

    for index, entry in enumerate(raw_rules):
        hint = f"{path} 규칙 {index + 1}번"
        if not isinstance(entry, dict):
            raise RuleConfigError(f"{hint}: 규칙 항목이 매핑이 아닙니다.")

        rule_id = entry.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise RuleConfigError(f"{hint}: id가 없습니다. 판정 로그에 남길 값입니다.")
        if rule_id in seen_ids:
            raise RuleConfigError(f"{hint}: 규칙 ID '{rule_id}'가 중복됩니다.")
        seen_ids.add(rule_id)

        label_text = entry.get("결과")
        try:
            label = Label(label_text)
        except ValueError:
            valid = ", ".join(label.value for label in Label)
            raise RuleConfigError(
                f"{hint} ({rule_id}): 결과 '{label_text}'는 없는 라벨입니다. 가능한 값: {valid}"
            ) from None

        conditions = entry.get("조건")
        if conditions is None:
            conditions = {}
        if not isinstance(conditions, dict):
            raise RuleConfigError(f"{hint} ({rule_id}): 조건이 매핑이 아닙니다.")

        # 조건 이름이 실제 입력값이나 등록된 파생 값인지 확인한다.
        # 오타 난 조건 이름이 조용히 무시되면 규칙이 통째로 동작하지 않는다.
        for name in conditions:
            if name not in _INPUT_FIELDS and name not in DERIVED_CONDITIONS:
                valid = ", ".join(sorted(_INPUT_FIELDS | set(DERIVED_CONDITIONS)))
                raise RuleConfigError(
                    f"{hint} ({rule_id}): 조건 이름 '{name}'은 판정 입력에 없습니다. "
                    f"가능한 이름: {valid}"
                )

            # 숫자 범위 조건이 참조하는 임계값 이름도 지금 확인한다.
            # 평가 시점까지 미루면 그 규칙에 처음 도달할 때야 터진다.
            expected = conditions[name]
            if isinstance(expected, dict):
                for bound in ("최소", "최대"):
                    if bound in expected:
                        _resolve_number(
                            expected[bound], thresholds, f"{hint} ({rule_id}) 조건 '{name}'"
                        )

        rules.append(
            Rule(
                id=rule_id,
                description=str(entry.get("설명", "")).strip(),
                conditions=conditions,
                label=label,
                rationale=str(entry.get("근거", "")).strip(),
                guidance=str(entry.get("안내", "")).strip(),
                usable_without_mfds=bool(entry.get("식약처_조회실패_판정", False)),
            )
        )

    _assert_conservative_default(rules, path)
    return RuleSet(version=version, thresholds=thresholds, rules=tuple(rules))


def _assert_conservative_default(rules: list[Rule], path: Path) -> None:
    """보수적 기본값을 구조로 강제한다.

    마지막 규칙은 조건이 비어 있고 결과가 UNKNOWN이어야 한다.
    이 검사가 없으면 규칙표를 고치다가 기본값이 사라져도 아무도 모른다.
    """
    last = rules[-1]
    if last.conditions:
        raise RuleConfigError(
            f"{path}: 마지막 규칙 {last.id}에 조건이 있습니다. "
            "어느 규칙에도 맞지 않는 입력을 받아 낼 기본 규칙이 없습니다."
        )
    if last.label is not Label.UNKNOWN:
        raise RuleConfigError(
            f"{path}: 마지막 기본 규칙 {last.id}의 결과가 {last.label}입니다. "
            "기본값은 반드시 UNKNOWN이어야 합니다. 오판보다 판정 보류가 안전합니다."
        )
    # 조건 없는 규칙이 중간에 있으면 그 뒤 규칙은 절대 평가되지 않는다.
    for rule in rules[:-1]:
        if not rule.conditions:
            raise RuleConfigError(
                f"{path}: 규칙 {rule.id}에 조건이 없어 뒤 규칙이 평가되지 않습니다."
            )


# ─────────────────────────────────────────────────────────────
# 판정 입력 만들기
# ─────────────────────────────────────────────────────────────


def build_verdict_input(
    *,
    claims: ClaimSet,
    content: ExtractedContent | None,
    academic: AcademicEvidence | None,
    mfds: MfdsEvidence | None,
    thresholds: dict[str, Any],
) -> VerdictInput:
    """④까지의 결과를 규칙 엔진이 보는 평평한 구조로 옮긴다.

    외부 조회가 실패했을 때 값을 낙관적으로 채우지 않는다.
    조회 실패는 recognition=UNKNOWN으로 떨어진다. 식약처를 확인하지 못했으므로
    믿을 만해요는 나오지 않는다. 논문 근거만으로 낼 수 있는 경고 쪽 판정은
    R-090·R-091이 맡고, 나머지는 R-999가 UNKNOWN을 돌려준다.
    """
    quality = _transcript_quality(content, thresholds)

    if mfds is None or mfds.lookup_failed:
        recognition = "UNKNOWN"
        functionality_match = "NOT_APPLICABLE"
        ad_violation_hit = False
    else:
        recognition = mfds.recognition
        functionality_match = mfds.match_level
        ad_violation_hit = mfds.ad_violation_hit

    if academic is None or academic.lookup_failed:
        supporting = 0
        refuting = 0
    else:
        supporting = academic.supporting
        refuting = academic.refuting

    return VerdictInput(
        in_scope=claims.in_scope,
        transcript_quality=quality,
        disease_cure_expression=claims.disease_cure_expression,
        ad_violation_hit=ad_violation_hit,
        prohibited_ad_expression=claims.prohibited_ad_expression,
        recognition=recognition,
        functionality_match=functionality_match,
        supporting_papers=supporting,
        refuting_papers=refuting,
    )


def _transcript_quality(content: ExtractedContent | None, thresholds: dict[str, Any]) -> str:
    """자막 상태를 규칙표의 transcript_quality 값으로 옮긴다."""
    if content is None or not content.has_transcript or not content.text.strip():
        return "none"
    floor = thresholds.get("자막_신뢰도_최저", 0.6)
    if content.confidence < floor:
        return "low"
    return "ok"
