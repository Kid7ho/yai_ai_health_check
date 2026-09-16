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
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from healthcheck.adapters.liner.academic import (  # noqa: E402
    _GENERIC_EFFECT_WORDS,
    _load_title_expressions,
    query_terms_of,
)
from healthcheck.pipeline import step1_input  # noqa: E402

DIR = ROOT / "labeling"
CANDIDATES = DIR / "candidates.yaml"
# 실제 블로그·기사·유튜브 링크. 본문은 한 번 추출해 link_contents.json에 저장하고 이후에는 그 본문만 쓴다.
# 글이 고쳐지거나 영상 자막 호출이 막혀도 팀원이 보는 사례가 바뀌지 않게 한다.
LINKS = DIR / "links.yaml"
LINK_CONTENTS = DIR / "link_contents.json"
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


# 성분 영문명에서 뺄 일반어. "Korean red ginseng"의 korean이 아무 논문 문장에나 걸린다.
_GENERIC_INGREDIENT_WORDS = frozenset({"korean", "red", "acids", "fatty", "extract", "supplementation", "complex"})
# 결과·결론을 말하는 문장 표지. 같은 점수면 이런 문장을 먼저 보여 준다.
_FINDING_CUES = re.compile(
    r"\b(conclusion|conclude|results?|found|showed|suggest|significant|no effect|not|improv|reduc|increas|decreas|associated|lower)",
    re.IGNORECASE,
)
# 초록에 섞여 들어온 키워드·인용 안내·저작권 줄은 발췌하지 않는다.
_METADATA = re.compile(r"^(key\s*words?|abstract$)|please cite|copyright|©", re.IGNORECASE)
# 발췌 문장 번역. scripts/translate_excerpts.py가 만든다. 시트 표시용이며 판정에 쓰지 않는다.
EXCERPT_KO = DIR / "excerpt_ko.json"
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")


def _stem(word: str) -> str:
    """3글자 이하는 그대로, 그보다 길면 앞 5글자. 논문 관련성 판단과 같은 기준이다."""
    return word if len(word) <= 3 else word[:5]


def excerpt(abstract: str, claim) -> dict:
    """초록에서 주장이 등장하는 문장을 고른다.

    팀원이 논문 전체를 읽지 않고도 주장이 어떤 맥락에서 나오는지 보게 하려는 것이다.
    효능 표현이 있는 문장을 우선하고, 성분 이름과 결과 표지가 함께 있으면 앞에 둔다.
    그런 문장이 없으면 결론 문장을 보여 주고 그 사실을 표시한다.
    """
    text = " ".join((abstract or "").split())
    if not text:
        return {"sentences": [], "terms": [], "basis": "초록 없음"}
    ingredient, effect = query_terms_of(claim)
    ingredient_terms = [_stem(w) for w in ingredient.lower().split() if w not in _GENERIC_INGREDIENT_WORDS]
    expressions = _load_title_expressions().get(effect.lower()) or (
        (" ".join(w for w in effect.lower().split() if w not in _GENERIC_EFFECT_WORDS),) if effect else ()
    )
    effect_sets = [[_stem(w) for w in expression.split()] for expression in expressions if expression]

    sentences = _SENTENCE_SPLIT.split(text)
    scored: list[tuple[int, int]] = []
    for index, sentence in enumerate(sentences):
        if _METADATA.search(sentence):
            continue
        lower = sentence.lower()
        has_effect = any(all(term in lower for term in terms) for terms in effect_sets)
        has_ingredient = any(term in lower for term in ingredient_terms)
        score = (2 if has_effect else 0) + (1 if has_ingredient else 0) + (1 if _FINDING_CUES.search(sentence) else 0)
        if has_effect:
            scored.append((score, index))

    if scored:
        chosen = sorted(index for _, index in sorted(scored, key=lambda s: (-s[0], s[1]))[:3])
        basis = "주장 단어가 나오는 문장"
    else:
        conclusion = [i for i, s in enumerate(sentences) if re.search(r"\bconclu", s, re.IGNORECASE)]
        chosen = conclusion[-1:] or [len(sentences) - 1]
        basis = "주장 단어가 없어 결론 문장"
    # 화면 강조용. 'vitamin d'의 d처럼 짧은 조각은 아무 단어에나 걸리므로 뺀다.
    terms = sorted(
        {t for t in ingredient_terms if len(t) >= 3} | {t for terms in effect_sets for t in terms if len(t) >= 3},
        key=len,
        reverse=True,
    )
    picked = [sentences[i] for i in chosen]
    return {"sentences": picked, "ko": [_translations().get(s, "") for s in picked], "terms": terms, "basis": basis}


_KO_CACHE: dict[str, str] | None = None


def _translations() -> dict[str, str]:
    global _KO_CACHE
    if _KO_CACHE is None:
        _KO_CACHE = json.loads(EXCERPT_KO.read_text(encoding="utf-8")) if EXCERPT_KO.is_file() else {}
    return _KO_CACHE


def recognition_row(mfds) -> str:
    if mfds is None or mfds.lookup_failed or mfds.recognition == "UNKNOWN":
        return "식약처 조회 실패"
    if mfds.recognition in ("INDIVIDUAL", "NOTIFIED"):
        return "인정+효능 일치" if mfds.match_level == "EXACT" else "인정+효능 일부 일치"
    return "미인정"


def claim_context(text: str, span: tuple[int, int], margin: int = 120) -> str:
    """주장 앞뒤 원문. 긴 글에서 주장이 어떤 맥락으로 나왔는지 보게 한다."""
    if not text or not span:
        return ""
    start, end = max(0, span[0] - margin), min(len(text), span[1] + margin)
    snippet = " ".join(text[start:end].split())
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")


def claim_view(check, content_text: str = "") -> dict:
    claim, mfds, academic = check.claim, check.mfds, check.academic
    return {
        "context": claim_context(content_text, claim.span) if len(content_text) > 200 else "",
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
                "abstract": p.description or "",
                "excerpt": excerpt(p.description or "", claim),
            }
            for p in academic.papers
        ],
        "svcCount": None
        if academic is None
        else {"s": academic.supporting, "r": academic.refuting, "failed": academic.lookup_failed},
    }


class SavedContentExtractor:
    """저장한 본문을 돌려주는 추출기. 녹화 모드에서 저장본이 없으면 실제 추출기로 받아 저장한다."""

    def __init__(self, store: dict, prefix: str, live=None) -> None:
        self._store = store
        self._prefix = prefix
        self._live = live

    async def fetch(self, key: str):
        from healthcheck.domain.types import ExtractedContent

        stored = self._store.get(f"{self._prefix}:{key}")
        if stored is not None:
            return ExtractedContent(**stored)
        if self._live is None:
            raise RuntimeError(f"저장된 본문이 없습니다. --mode record로 다시 만드세요: {self._prefix}:{key}")
        content = await self._live.fetch(key)
        self._store[f"{self._prefix}:{key}"] = {
            "text": content.text,
            "has_transcript": content.has_transcript,
            "confidence": content.confidence,
            "language": content.language,
            "content_hash": content.content_hash,
            "source": content.source,
        }
        return content


def load_inputs() -> list[tuple[str, str]]:
    """(묶음, 입력) 목록. 후보 문장 뒤에 실제 링크를 붙인다."""
    raw = yaml.safe_load(CANDIDATES.read_text(encoding="utf-8")) or {}
    inputs = [(group, sentence) for group, sentences in raw.items() for sentence in sentences]
    if LINKS.is_file():
        for link in yaml.safe_load(LINKS.read_text(encoding="utf-8")) or []:
            inputs.append(("실제 링크", link["url"]))
    return inputs


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

    store = json.loads(LINK_CONTENTS.read_text(encoding="utf-8")) if LINK_CONTENTS.is_file() else {}
    live_transcript = live_article = None
    if mode == "record":
        from dataclasses import replace

        from healthcheck.api.app import build_orchestrator

        live = build_orchestrator(replace(settings, adapter_mode="live"))
        live_transcript, live_article = live.transcript, live.article
    orchestrator.transcript = SavedContentExtractor(store, "youtube", live_transcript)
    orchestrator.article = SavedContentExtractor(store, "article", live_article)

    results: list[dict] = []
    for group, text in load_inputs():
        try:
            result = await orchestrator.run(step1_input.classify(text))
        except Exception as error:  # 링크 하나가 막혀도 나머지는 만든다
            print(f"⚠ 건너뜀 {text}: {type(error).__name__} {str(error)[:120]}")
            continue
        results.append({"group": group, "input": text, "result": result})
    if mode == "record":
        transport.save()
        LINK_CONTENTS.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
    return results


def build(results: list[dict]) -> tuple[list[dict], list[dict], dict]:
    pool: dict[tuple[str, str], list[dict]] = defaultdict(list)
    skipped: dict[str, int] = defaultdict(int)
    link_cases: list[dict] = []
    link_cells: list[dict] = []
    for item in results:
        result = item["result"]
        if item["group"] == "실제 링크":
            # 실제 링크는 칸을 가리지 않고 모두 넣는다. 주장이 여럿이면 주장마다 칸을 적는다.
            text = result.content.text if result.content else ""
            case_id = f"link-{len(link_cases) + 1:02d}"
            # 주장이 없어도 넣는다. 관문 규칙(범위 밖·질병 치료·금지 광고)에서 멈췄거나 주장을 놓친 경우를
            # 팀원이 원문을 보고 판단한다. 이런 사례는 주장 대신 원문 앞부분을 보여 준다.
            views = [claim_view(check, text) for check in result.claim_checks] or [
                {
                    "noClaim": True,
                    "ingredient": "",
                    "effect": "",
                    "intensity": "",
                    "statement": "",
                    "context": " ".join(text.split())[:600] + ("…" if len(text) > 600 else ""),
                    "mfds": None,
                    "papers": [],
                    "svcCount": None,
                }
            ]
            link_cases.append({"id": case_id, "source": "실제 링크", "input": item["input"], "claims": views})
            link_cells.append(
                {
                    "id": case_id,
                    "service_label": result.verdict.label,
                    "service_rule": result.verdict.rule_id,
                    "content_source": result.content.source if result.content else None,
                    "claims": [
                        {
                            "row": recognition_row(check.mfds),
                            "grade": evidence_grade(check.academic.supporting, check.academic.refuting)
                            if check.academic and not check.academic.lookup_failed
                            else "학술 검색 실패",
                            "service_label": check.verdict.label,
                            "service_rule": check.verdict.rule_id,
                        }
                        for check in result.claim_checks
                    ],
                }
            )
            continue
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
    cases.extend(link_cases)
    cells.extend(link_cells)
    return cases, cells, {"counts": counts, "skipped": dict(skipped), "pool": {f"{r}/{g}": len(v) for (r, g), v in pool.items()}}


def case_key(case: dict) -> str:
    """사례의 변하지 않는 식별 값. 같은 문장·링크라도 조회 실패 가정 사례는 따로 센다."""
    return f"{case['source']}|{case['input']}"


def merge_published(cases: list[dict], cells: list[dict]) -> tuple[list[dict], list[dict], int]:
    """이미 내보낸 사례는 번호와 내용을 그대로 두고 새 사례만 뒤에 붙인다.

    팀원 분류는 사례 번호(lab-001)와 주장 순서로 저장된다. 후보를 늘리거나 추출 코드를 고친 뒤
    다시 만들면 번호가 밀리거나 주장이 바뀌어, 이미 한 분류가 다른 사례에 붙거나 사라진다.
    그래서 한 번 내보낸 사례는 고정한다. 이번 선정에서 빠진 사례도 지우지 않는다.
    """
    path = DIR / "cases.json"
    if not path.is_file():
        return cases, cells, len(cases)
    published = json.loads(path.read_text(encoding="utf-8"))
    published_cells = json.loads((DIR / "cells.json").read_text(encoding="utf-8")) if (DIR / "cells.json").is_file() else []
    known = {case_key(case) for case in published}
    width = {"lab": 3, "link": 2}
    next_number = {prefix: 0 for prefix in width}
    for case in published:
        prefix, number = case["id"].split("-", 1)
        next_number[prefix] = max(next_number.get(prefix, 0), int(number))

    cells_by_id = {cell["id"]: cell for cell in cells}
    merged_cases, merged_cells = list(published), list(published_cells)
    added = 0
    for case in cases:
        if case_key(case) in known:
            continue
        prefix = case["id"].split("-", 1)[0]
        next_number[prefix] = next_number.get(prefix, 0) + 1
        new_id = f"{prefix}-{next_number[prefix]:0{width.get(prefix, 3)}d}"
        cell = dict(cells_by_id.get(case["id"], {}), id=new_id)
        merged_cases.append(dict(case, id=new_id))
        merged_cells.append(cell)
        added += 1
    return merged_cases, merged_cells, added


def main() -> int:
    parser = argparse.ArgumentParser(description="분류 시트 사례를 만든다.")
    parser.add_argument("--mode", choices=["record", "replay"], default="replay")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="이미 내보낸 사례를 버리고 처음부터 만든다. 팀원 분류가 시작된 뒤에는 쓰지 않는다",
    )
    args = parser.parse_args()
    logging.disable(logging.WARNING)

    if args.mode == "record":
        asyncio.run(run("record"))
    # 녹화 직후에도 재생으로 만든다. 시트에 들어가는 근거가 녹화 파일과 정확히 같아야 한다.
    results = asyncio.run(run("replay"))
    cases, cells, summary = build(results)
    if not args.reset:
        cases, cells, added = merge_published(cases, cells)
        print(f"기존 사례 유지, 새 사례 {added}건 추가")
    # 번역은 표시용이라 고정한 사례에도 새로 채운다. 발췌 문장 자체는 바꾸지 않는다.
    for case in cases:
        for claim in case["claims"]:
            for paper in claim.get("papers", []):
                excerpt_info = paper.get("excerpt") or {}
                excerpt_info["ko"] = [_translations().get(s, "") for s in excerpt_info.get("sentences", [])]
    (DIR / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    (DIR / "cells.json").write_text(json.dumps(cells, ensure_ascii=False, indent=1), encoding="utf-8")
    # 시트 HTML에 사례를 넣는다. </script>가 끼어 스크립트가 끊기지 않게 '</'를 가린다.
    embedded = json.dumps(cases, ensure_ascii=False).replace("</", "<\\/")
    template = (DIR / "sheet_template.html").read_text(encoding="utf-8")
    (DIR / "claim_labeling.html").write_text(template.replace("/*CASES*/", embedded), encoding="utf-8")

    print(f"후보 {len(results)}건 → 사례 {len(cases)}건")
    print(f"제외: {summary['skipped']}")
    links = [c for c in cases if c["source"] == "실제 링크"]
    print(f"실제 링크 사례 {len(links)}건, 주장 {sum(len(c['claims']) for c in links)}개")
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
