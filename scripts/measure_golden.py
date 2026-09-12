#!/usr/bin/env python3
"""골든셋 정확도 측정.

원문을 파이프라인에 넣어 나온 라벨을 기대 라벨과 비교한다. 이 프로젝트의
품질 기준은 스펙 구현 여부가 아니라 골든셋 통과 여부다.

pytest의 골든 러너와 역할이 다르다. 러너는 판정입력이 적힌 케이스만 돌려
규칙 엔진을 회귀 검증한다(네트워크 없음). 이 스크립트는 원문에서 시작해
③ 구조화와 ④ 검증까지 전 구간을 재므로 외부 API를 쓴다.

사용법
    python scripts/measure_golden.py                 # 실제 API
    python scripts/measure_golden.py --mode fake     # 토큰 안 씀
    python scripts/measure_golden.py --only gold-114
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402

from healthcheck.api.app import build_fake_orchestrator, build_orchestrator  # noqa: E402
from healthcheck.config import load_settings  # noqa: E402
from healthcheck.pipeline import step1_input  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "tests" / "golden"


def load_cases(only: str | None) -> list[dict]:
    cases: list[dict] = []
    for path in sorted(GOLDEN_DIR.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        for entry in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            entry["_file"] = path.name
            if only is None or entry["id"] == only:
                cases.append(entry)
    return cases


async def main() -> int:
    parser = argparse.ArgumentParser(description="골든셋 정확도를 측정한다.")
    parser.add_argument("--mode", choices=["live", "fake"], default="live")
    parser.add_argument("--only", help="케이스 하나만 돌린다")
    args = parser.parse_args()

    orchestrator = (
        build_orchestrator(load_settings()) if args.mode == "live" else build_fake_orchestrator()
    )

    cases = load_cases(args.only)
    print(f"골든 케이스 {len(cases)}건 / 모드 {args.mode} / 규칙표 {orchestrator.ruleset.version}")
    print(
        f"어댑터: 자막 {type(orchestrator.transcript).__name__}, "
        f"학술 {type(orchestrator.academic).__name__}, 식약처 {type(orchestrator.mfds).__name__}"
    )
    print()

    passed: list[dict] = []
    failed: list[dict] = []
    confusion: Counter = Counter()

    for case in cases:
        result = await orchestrator.run(step1_input.classify(case["input"]))
        actual = result.verdict.label.value
        expected = case["expected"]
        confusion[(expected, actual)] += 1

        mark = "○" if actual == expected else "✗"
        row = {
            "id": case["id"],
            "input": case["input"],
            "expected": expected,
            "actual": actual,
            "rule": result.verdict.rule_id,
            "reason": case.get("reason", ""),
            "claims": [
                f"{claim.ingredient}/{claim.claimed_effect[:26]}"
                for claim in (result.claims.claims if result.claims else ())
            ],
            "mfds": (
                f"{result.mfds.recognition}/{result.mfds.match_level}" if result.mfds else "-"
            ),
            "papers": (
                f"{result.academic.supporting}/{result.academic.refuting}"
                if result.academic
                else "-"
            ),
        }
        (passed if actual == expected else failed).append(row)
        print(
            f"{mark} {case['id']:9s} 기대 {expected:12s} 실제 {actual:12s} {row['rule']:7s} "
            f"{case['input'][:40]}"
        )

    total = len(cases)
    print()
    print(f"정확도 {len(passed)}/{total} ({len(passed) / total * 100:.0f}%)")

    if failed:
        print("\n■ 틀린 케이스")
        for row in failed:
            print(f"\n  {row['id']}  기대 {row['expected']} / 실제 {row['actual']} ({row['rule']})")
            print(f"    원문: {row['input']}")
            print(f"    뽑은 주장: {row['claims'] or '없음'}")
            print(f"    식약처: {row['mfds']} / 논문(뒷받침·반박): {row['papers']}")
            print(f"    기대 이유: {row['reason'].strip()}")

    print("\n■ 라벨별 (기대 -> 실제)")
    for (expected, actual), count in sorted(confusion.items()):
        flag = "" if expected == actual else "  ← 불일치"
        print(f"  {expected:12s} -> {actual:12s} {count}건{flag}")

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
