#!/usr/bin/env python3
"""의료 자문 시트를 만든다.

팀원 분류 시트(labeling/cases.json) 중 labeling/advisory_cases.yaml에 고른 20건을 녹화 재생으로 다시 돌려,
팀원 시트에서는 가렸던 서비스의 판단(주장별 신호등·규칙, 식약처 일치 수준, 논문마다 뒷받침·반박 분류)을
함께 담는다. 교수님은 이 판단이 맞는지를 단계마다 고른다.

    .venv/bin/python scripts/build_advisory_sheet.py

산출물
    labeling/advisory_data.json   시트에 넣는 사례(검토용 사본)
    labeling/advisory_sheet.html  서버가 /labeling/{토큰}/ 에서 여는 시트
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import yaml  # noqa: E402

import build_labeling_cases as labeling  # noqa: E402

DIR = ROOT / "labeling"
SELECTION = DIR / "advisory_cases.yaml"
RULES = ROOT / "rules" / "verdict_rules_v0.yaml"
MATCH_KO = {"EXACT": "일치", "PARTIAL": "일부 일치", "MISMATCH": "다름", "NONE": "비교 대상 없음"}


def rule_book() -> tuple[str, dict[str, dict]]:
    raw = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    return raw["version"], {rule["id"]: rule for rule in raw["규칙"]}


def verdict_view(verdict, rules: dict[str, dict]) -> dict:
    """판정과 그 판정을 낸 규칙. 교수님이 규칙의 설명·근거까지 보고 검수하게 한다."""
    rule = rules.get(verdict.rule_id, {})
    return {
        "label": verdict.label,
        "rule": verdict.rule_id,
        "guidance": verdict.guidance or rule.get("안내", ""),
        "desc": rule.get("설명", ""),
        "basis": " ".join(str(rule.get("근거", "")).split()),
        "evidence": list(verdict.evidence),
    }


def trigger_sentences(text: str, rule_id: str) -> list[str]:
    """관문 규칙이 걸린 문장. 규칙표 조건에는 문장이 남지 않아 추출기와 같은 기준으로 다시 찾는다."""
    from healthcheck.pipeline import step3_claims as step3

    extractor = step3.RuleBasedClaimExtractor()
    patterns = extractor._patterns
    found: list[str] = []
    for sentence, _ in step3._sentences(step3._blank_reference_lines(text or "")):
        if rule_id == "R-021":
            hit = not step3._contains_any(sentence, patterns.negation) and extractor._is_disease_cure(sentence)
        elif rule_id == "R-001":
            hit = step3._contains_refusal(sentence, patterns.out_of_scope_immediate, patterns.out_of_scope_excluded_words)
        else:
            hit = False
        if hit:
            found.append(" ".join(sentence.split())[:300])
    return found[:5]


def claim_advisory(check, content_text: str, rules: dict[str, dict]) -> dict:
    view = labeling.claim_view(check, content_text)
    if check.academic is not None:
        # 팀원 시트는 서비스의 논문 분류를 가렸다. 자문 시트는 그 분류가 맞는지 묻는 것이므로 보여 준다.
        for paper_view, paper in zip(view["papers"], check.academic.papers):
            paper_view["svc"] = paper.stance
    if view["mfds"] is not None and check.mfds is not None:
        view["mfds"]["match"] = check.mfds.match_level
        view["mfds"]["matchKo"] = MATCH_KO.get(check.mfds.match_level, check.mfds.match_level)
    view["verdict"] = verdict_view(check.verdict, rules)
    return view


def main() -> int:
    logging.disable(logging.WARNING)
    selection = yaml.safe_load(SELECTION.read_text(encoding="utf-8"))
    published = {case["id"]: case for case in json.loads((DIR / "cases.json").read_text(encoding="utf-8"))}
    cells = {cell["id"]: cell for cell in json.loads((DIR / "cells.json").read_text(encoding="utf-8"))}
    picks = selection["사례"]
    missing = [pick["id"] for pick in picks if pick["id"] not in published]
    if missing:
        raise SystemExit(f"cases.json에 없는 사례: {missing}")

    # 고른 사례의 입력만 재생한다. 녹화에 없는 요청이 있으면 build_labeling_cases.run이 멈춘다.
    inputs = [("실제 링크" if published[p["id"]]["source"] == "실제 링크" else "후보 문장", published[p["id"]]["input"]) for p in picks]
    labeling.load_inputs = lambda: inputs
    results = {item["input"]: item["result"] for item in asyncio.run(labeling.run("replay"))}

    version, rules = rule_book()
    cases, drift = [], []
    for pick in picks:
        case = published[pick["id"]]
        result = results.get(case["input"])
        if result is None:
            raise SystemExit(f"재생 결과가 없습니다: {pick['id']} {case['input']}")
        text = result.content.text if result.content else ""
        claims = [claim_advisory(check, text if case["source"] == "실제 링크" else "", rules) for check in result.claim_checks]
        if not claims:
            claims = [dict(case["claims"][0], verdict=None)]
        # 번역은 팀원 시트에 이미 채워 둔 것을 쓴다(같은 발췌 문장이면 같은 번역).
        for claim in claims:
            for paper in claim.get("papers", []):
                info = paper.get("excerpt") or {}
                info["ko"] = [labeling._translations().get(s, "") for s in info.get("sentences", [])]
        cell = cells.get(pick["id"], {})
        if cell.get("service_rule") and cell["service_rule"] != result.verdict.rule_id:
            drift.append(f"{pick['id']}: 시트 {cell['service_rule']} → 지금 {result.verdict.rule_id}")
        cases.append(
            {
                "id": pick["id"],
                "topic": pick["주제"],
                "question": pick["질문"],
                "source": case["source"],
                "input": case["input"],
                "contentSource": result.content.source if result.content else None,
                "verdict": dict(verdict_view(result.verdict, rules), triggers=trigger_sentences(text or case["input"], result.verdict.rule_id)),
                "claims": claims,
            }
        )

    data = {"rulesVersion": version, "topics": selection["주제"], "cases": cases}
    (DIR / "advisory_data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    embedded = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    template = (DIR / "advisory_template.html").read_text(encoding="utf-8")
    (DIR / "advisory_sheet.html").write_text(template.replace("/*DATA*/", embedded), encoding="utf-8")

    print(f"사례 {len(cases)}건, 주장 {sum(len(c['claims']) for c in cases)}개, 규칙표 {version}")
    for case in cases:
        v = case["verdict"]
        print(f"  {case['id']:8} {v['label']:11} {v['rule']:6} " + " / ".join(
            f"{c.get('ingredient') or '주장 없음'}:{c['verdict']['label'] if c.get('verdict') else '-'}" for c in case["claims"]
        ))
    if drift:
        print("⚠ 팀원 시트를 만들 때와 판정이 달라진 사례:\n  " + "\n  ".join(drift))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
