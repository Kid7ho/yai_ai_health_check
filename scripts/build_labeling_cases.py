#!/usr/bin/env python3
"""분류 시트 사례를 만든다.

labeling/candidates.yaml의 문장을 실제 파이프라인(주장 추출 → 식약처 → Liner)으로 돌려
판정표 16칸(국가 인정 4줄 × 논문 근거 등급 4열)으로 나누고, 칸마다 골고루 뽑는다.

학술·식약처 응답은 labeling/recordings.json에 녹화한다. 팀원 다섯 명이 같은 근거를 보고
분류해야 하므로 한 번 녹화한 뒤에는 재생으로만 만든다. 골든셋 녹화와 섞지 않는다.

    .venv/bin/python scripts/build_labeling_cases.py --mode record   # 19시~9시, 키 필요
    .venv/bin/python scripts/build_labeling_cases.py                 # 녹화 재생

산출물
    labeling/cases.json  시트에 넣는 사례. 서비스 판정·칸·논문 입장 분류는 넣지 않는다
    labeling/cells.json  사례별 칸과 서비스 판정. 분류 결과 분석용이며 팀원에게 보여 주지 않는다
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from healthcheck.pipeline import step1_input  # noqa: E402

DIR = ROOT / "labeling"
CANDIDATES = DIR / "candidates.yaml"
RECORDINGS = DIR / "recordings.json"

ROWS = ("인정+효능 일치", "인정+효능 일부 일치", "미인정", "식약처 조회 실패")
GRADES = ("뒷받침 우세", "엇갈림", "반박 우세", "근거 부족")
PER_CELL = 4
RECOGNITION_KO = {"INDIVIDUAL": "개별인정", "NOTIFIED": "고시형", "NOT_RECOGNIZED": "미인정", "UNKNOWN": "조회 실패"}
# 관문 규칙에서 멈춘 판정은 16칸 판정표의 대상이 아니다.
GATE_RULES = {"R-001", "R-010", "R-020", "R-021", "R-030", "R-031"}


def evidence_grade(supporting: int, refuting: int) -> str:
    """논문 근거 등급 초안(2026-09-16). 2편 이상이고 상대의 2배보다 많으면 우세다."""
    if supporting <= 1 and refuting <= 1:
        return "근거 부족"
    if supporting >= 2 and supporting > 2 * refuting:
        return "뒷받침 우세"
    if refuting >= 2 and refuting > 2 * supporting:
        return "반박 우세"
    return "엇갈림"


def recognition_row(mfds) -> str:
    if mfds is None or mfds.lookup_failed or mfds.recognition == "UNKNOWN":
        return "식약처 조회 실패"
    if mfds.recognition in ("INDIVIDUAL", "NOTIFIED"):
        return "인정+효능 일치" if mfds.match_level == "EXACT" else "인정+효능 일부 일치"
    return "미인정"


def claim_view(check) -> dict:
    claim, mfds, academic = check.claim, check.mfds, check.academic
    return {
        "ingredient": claim.ingredient,
        "effect": claim.claimed_effect,
        "intensity": claim.intensity,
        "statement": claim.statement or "",
        "mfds": None
        if mfds is None
        else {
            "recognition": RECOGNITION_KO.get(mfds.recognition, mfds.recognition),
            "text": mfds.functionality_text,
            "no": mfds.recognition_no,
            "others": list(mfds.other_recognitions),
            "failed": mfds.lookup_failed,
        },
        "papers": []
        if academic is None
        else [
            {
                "title": p.title,
                "url": p.url,
                "journal": p.journal,
                "date": p.date,
                "citations": p.citation_count,
                "abstract": (p.description or "")[:600],
            }
            for p in academic.papers
        ],
        "svcCount": None
        if academic is None
        else {"s": academic.supporting, "r": academic.refuting, "failed": academic.lookup_failed},
    }


async def run(mode: str) -> list[dict]:
    from healthcheck.testing.replay import replay_orchestrator

    if mode == "record":
        from healthcheck.adapters.mfds.snapshot import in_restricted_hours
        from healthcheck.config import load_settings

        if in_restricted_hours():
            raise SystemExit("식품안전나라 API 제한 시간(09시~19시)에는 녹화하지 않습니다.")
        settings = load_settings()
        orchestrator, transport = replay_orchestrator(
            RECORDINGS,
            "record",
            liner_key=settings.liner_api_key or "",
            mfds_key=settings.mfds_api_key or "",
            reuse_recorded=True,
        )
    else:
        orchestrator, transport = replay_orchestrator(RECORDINGS, "replay")

    raw = yaml.safe_load(CANDIDATES.read_text(encoding="utf-8")) or {}
    results: list[dict] = []
    for group, sentences in raw.items():
        for sentence in sentences:
            result = await orchestrator.run(step1_input.classify(sentence))
            results.append({"group": group, "input": sentence, "result": result})
    if mode == "record":
        transport.save()
    return results


def build(results: list[dict]) -> tuple[list[dict], list[dict], dict]:
    pool: dict[tuple[str, str], list[dict]] = defaultdict(list)
    skipped: dict[str, int] = defaultdict(int)
    for item in results:
        result = item["result"]
        if result.verdict.rule_id in GATE_RULES:
            skipped[f"관문 규칙 {result.verdict.rule_id}"] += 1
            continue
        if not result.claim_checks:
            skipped["주장 없음"] += 1
            continue
        # 한 문장에서 주장이 하나일 때만 칸을 정한다. 여러 주장이면 칸이 섞인다.
        if len(result.claim_checks) != 1:
            skipped["주장 여러 개"] += 1
            continue
        check = result.claim_checks[0]
        academic = check.academic
        if academic is None or academic.lookup_failed:
            skipped["학술 검색 실패"] += 1
            continue
        row = recognition_row(check.mfds)
        grade = evidence_grade(academic.supporting, academic.refuting)
        pool[(row, grade)].append(
            {"item": item, "check": check, "row": row, "grade": grade, "verdict": result.verdict}
        )

    # 식약처 조회 실패 줄은 실제로 만들 수 없다(사본이 있어 실패하지 않는다).
    # 같은 논문 근거에 식약처 결과만 '조회 실패'로 가정한 사례를 만든다.
    for grade in GRADES:
        donors = [e for (row, g), entries in pool.items() if g == grade and row != "식약처 조회 실패" for e in entries]
        pool[("식약처 조회 실패", grade)].extend({**e, "row": "식약처 조회 실패", "assumed": True} for e in donors)

    cases: list[dict] = []
    cells: list[dict] = []
    used_inputs: set[tuple[str, str]] = set()
    # 조회 실패 가정 사례는 다른 칸에 나오지 않은 문장을 먼저 쓴다. 같은 문장을 두 번 보면 앞의 답에 끌린다.
    used_any: set[str] = set()
    counts: dict[str, dict[str, int]] = {row: {grade: 0 for grade in GRADES} for row in ROWS}
    number = 0
    for row in ROWS:
        for grade in GRADES:
            chosen = 0
            seen_ingredients: set[str] = set()
            entries = pool.get((row, grade), [])
            # 성분이 겹치지 않게 먼저 한 바퀴 돌고, 모자라면 겹쳐도 채운다.
            for allow_repeat in (False, True):
                for entry in entries:
                    if chosen >= PER_CELL:
                        break
                    key = (entry["item"]["input"], row)
                    ingredient = entry["check"].claim.ingredient
                    if key in used_inputs or (not allow_repeat and ingredient in seen_ingredients):
                        continue
                    if entry.get("assumed") and not allow_repeat and entry["item"]["input"] in used_any:
                        continue
                    used_inputs.add(key)
                    used_any.add(entry["item"]["input"])
                    seen_ingredients.add(ingredient)
                    chosen += 1
                    number += 1
                    case_id = f"lab-{number:03d}"
                    view = claim_view(entry["check"])
                    if entry.get("assumed"):
                        view["mfds"] = {"recognition": "조회 실패", "text": None, "no": None, "others": [], "failed": True}
                    cases.append(
                        {
                            "id": case_id,
                            "source": "조회 실패 가정" if entry.get("assumed") else "후보 문장",
                            "input": entry["item"]["input"],
                            "claims": [view],
                        }
                    )
                    cells.append(
                        {
                            "id": case_id,
                            "row": row,
                            "grade": grade,
                            "assumed_mfds_failure": bool(entry.get("assumed")),
                            "group": entry["item"]["group"],
                            "service_label": None if entry.get("assumed") else entry["verdict"].label,
                            "service_rule": None if entry.get("assumed") else entry["verdict"].rule_id,
                            "supporting": entry["check"].academic.supporting,
                            "refuting": entry["check"].academic.refuting,
                        }
                    )
            counts[row][grade] = chosen
    return cases, cells, {"counts": counts, "skipped": dict(skipped), "pool": {f"{r}/{g}": len(v) for (r, g), v in pool.items()}}


def main() -> int:
    parser = argparse.ArgumentParser(description="분류 시트 사례를 만든다.")
    parser.add_argument("--mode", choices=["record", "replay"], default="replay")
    args = parser.parse_args()
    logging.disable(logging.WARNING)

    if args.mode == "record":
        asyncio.run(run("record"))
    # 녹화 직후에도 재생으로 만든다. 시트에 들어가는 근거가 녹화 파일과 정확히 같아야 한다.
    results = asyncio.run(run("replay"))
    cases, cells, summary = build(results)
    (DIR / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    (DIR / "cells.json").write_text(json.dumps(cells, ensure_ascii=False, indent=1), encoding="utf-8")
    # 시트 HTML에 사례를 넣는다. </script>가 끼어 스크립트가 끊기지 않게 '</'를 가린다.
    embedded = json.dumps(cases, ensure_ascii=False).replace("</", "<\\/")
    template = (DIR / "sheet_template.html").read_text(encoding="utf-8")
    (DIR / "claim_labeling.html").write_text(template.replace("/*CASES*/", embedded), encoding="utf-8")

    print(f"후보 {len(results)}건 → 사례 {len(cases)}건")
    print(f"제외: {summary['skipped']}")
    print("\n칸별 사례 수 (후보 수)")
    print("국가 인정 \\ 논문 근거".ljust(22) + "".join(g.ljust(10) for g in GRADES))
    for row in ROWS:
        line = row.ljust(22)
        for grade in GRADES:
            line += f"{summary['counts'][row][grade]} ({summary['pool'].get(f'{row}/{grade}', 0)})".ljust(10)
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
