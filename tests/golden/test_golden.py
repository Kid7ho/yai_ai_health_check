"""골든 테스트셋 러너.

tests/golden/*.yaml을 읽어 케이스마다 테스트 하나를 만든다.
이 프로젝트의 품질 기준은 스펙 구현 여부가 아니라 이 테스트의 통과 여부다.

지금은 ③ 주장 구조화와 ④ 이중 검증이 없으므로 케이스의 '판정입력' 절을
규칙 엔진에 그대로 넣는다. ③·④가 구현되면 input에서 판정입력을 직접
만들어 내도록 이 러너를 바꾸고, 손으로 적은 판정입력은 중간 산출물의
기대값으로 역할이 바뀐다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from healthcheck.domain.labels import Label
from healthcheck.domain.types import VerdictInput
from healthcheck.pipeline.step5_verdict import load_ruleset

GOLDEN_DIR = Path(__file__).parent
REQUIRED_KEYS = ("id", "input", "expected", "reason")


def _load_cases() -> list[dict[str, Any]]:
    """골든 케이스 전부를 읽는다. 앞에 밑줄이 붙은 파일은 템플릿이라 건너뛴다."""
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for path in sorted(GOLDEN_DIR.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(entries, list):
            raise AssertionError(f"{path.name}: 최상위가 목록이 아닙니다.")
        for entry in entries:
            missing = [key for key in REQUIRED_KEYS if not entry.get(key)]
            if missing:
                raise AssertionError(f"{path.name}: {entry.get('id', '?')}에 {missing} 항목이 없습니다.")
            if entry["id"] in seen_ids:
                raise AssertionError(f"골든 케이스 ID '{entry['id']}'가 중복됩니다.")
            seen_ids.add(entry["id"])
            entry["_file"] = path.name
            cases.append(entry)

    return cases


CASES = _load_cases()


def test_골든셋이_비어_있지_않다():
    """케이스가 하나도 없으면 품질 기준이 없는 것과 같다."""
    assert CASES, "골든 케이스가 없습니다."


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_골든_케이스(case: dict[str, Any]):
    given = case.get("판정입력")
    if not given:
        pytest.skip(f"{case['id']}: 판정입력이 아직 채워지지 않았습니다.")

    ruleset = load_ruleset()
    verdict = ruleset.evaluate(VerdictInput(**given))

    expected = Label(case["expected"])
    assert verdict.label is expected, (
        f"{case['id']} ({case['_file']}) 판정이 다릅니다.\n"
        f"  원문: {case['input']}\n"
        f"  기대: {expected} / 실제: {verdict.label} (규칙 {verdict.rule_id})\n"
        f"  이유: {case['reason']}"
    )

    if case.get("expected_rule"):
        assert verdict.rule_id == case["expected_rule"], (
            f"{case['id']}: 라벨은 맞지만 매칭된 규칙이 다릅니다. "
            f"기대 {case['expected_rule']} / 실제 {verdict.rule_id}"
        )
