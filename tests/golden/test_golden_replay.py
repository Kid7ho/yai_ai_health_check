"""녹화한 외부 응답으로 골든셋 전 구간을 잰다.

원문 → 주장 추출 → 학술·식약처 검증 → 판정까지 돈다. 외부 응답이 고정돼 있어
실패하면 코드나 규칙표를 고친 탓이다. 녹화는 scripts/measure_golden.py --mode record.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from healthcheck.pipeline import step1_input

GOLDEN_DIR = Path(__file__).resolve().parent
RECORDING = GOLDEN_DIR / "recordings.json"


def _cases() -> list[dict]:
    cases: list[dict] = []
    for path in sorted(GOLDEN_DIR.glob("*.yaml")):
        cases.extend(yaml.safe_load(path.read_text(encoding="utf-8")) or [])
    return cases


@pytest.mark.skipif(not RECORDING.is_file(), reason="녹화 파일이 없다. 19시 이후 --mode record로 만든다.")
async def test_녹화한_응답으로_골든셋_전체가_일치한다():
    from healthcheck.testing.replay import replay_orchestrator

    orchestrator, _ = replay_orchestrator(RECORDING, "replay")
    failures = []
    for case in _cases():
        result = await orchestrator.run(step1_input.classify(case["input"]))
        if result.verdict.label.value != case["expected"]:
            failures.append(f"{case['id']} 기대 {case['expected']} 실제 {result.verdict.label.value} ({result.verdict.rule_id})")
    assert not failures, "\n".join(failures)
