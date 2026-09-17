#!/usr/bin/env python3
"""주장 추출 정확도를 추출 평가셋(실제 링크 45건)으로 잰다.

규칙 기반과 언어 모델 추출을 같은 기준으로 비교하려고 만든다. 판정이 아니라 ③ 추출만 본다.

    .venv/bin/python scripts/measure_extraction.py                         # 규칙 기반
    .venv/bin/python scripts/measure_extraction.py --extractor llm --model fake
    .venv/bin/python scripts/measure_extraction.py --extractor llm --model gemini --verbose

지표
    핵심 재현율   정답 핵심 주장(사전 안 성분) 중 뽑은 비율
    전체 재현율   정답 주장(사전 안 성분) 중 뽑은 비율. 사전 밖 성분은 따로 센다
    정밀도       뽑은 주장 중 정답과 맞는 비율
    핵심 검증     핵심 주장이 실제 검증 대상(최대 3건)에 든 비율
    표시         질병 치료·금지 광고·약 대체 표시의 적중(tp)·놓침(fn)·오탐(fp)

정답은 사람 수준 판단을 흉내 낸 AI 표시이며 팀원 검수 전이다(annotations.json 참고).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from healthcheck.pipeline.orchestrator import MAX_VERIFIED_CLAIMS  # noqa: E402
from healthcheck.pipeline.step3_claims import RuleBasedClaimExtractor, load_patterns  # noqa: E402

EVAL_PATH = ROOT / "tests" / "extraction_eval" / "annotations.json"
CONTENTS_PATH = ROOT / "labeling" / "link_contents.json"

# 정답 효능 표기와 서비스 효능 표기를 맞춰 보기 위한 같은뜻 묶음. 측정에만 쓴다.
_SYNONYMS = {
    "혈액순환": ["혈행", "혈액흐름", "혈관"],
    "장": ["장 건강", "배변활동", "변비", "장내"],
    "면역력": ["면역", "면역 기능"],
    "뼈": ["골다공증", "골밀도"],
    "관절": ["관절염", "연골"],
    "혈당": ["당뇨"],
    "혈압": ["고혈압"],
    "눈": ["황반", "시력", "눈 건강", "노안"],
    "콜레스테롤": ["중성지질", "혈중 지질", "고지혈증"],
    "기억력": ["치매", "인지기능", "뇌"],
    "체지방": ["다이어트", "체중"],
}


def _targets(text: str, effect_targets: tuple[str, ...]) -> set[str]:
    found = {t for t in effect_targets if t in text}
    for target, words in _SYNONYMS.items():
        if target in text or any(word in text for word in words):
            found.add(target)
    return found


def _same_ingredient(gold: str, service: str) -> bool:
    """사전 밖 성분은 표기가 조금씩 달라("등푸른생선", "등푸른 생선") 한쪽이 다른 쪽을 포함하면 같다고 본다."""
    a, b = gold.replace(" ", ""), service.replace(" ", "")
    return a == b or (len(a) >= 2 and len(b) >= 2 and (a in b or b in a))


def _matches(gold: dict, claim, effect_targets: tuple[str, ...]) -> bool:
    if not _same_ingredient(gold["ingredient"], claim.ingredient):
        return False
    gold_targets = _targets(gold["effect"], effect_targets)
    claim_targets = _targets(claim.claimed_effect, effect_targets)
    if gold_targets and claim_targets:
        return bool(gold_targets & claim_targets)
    return gold["effect"].replace(" ", "")[:2] in claim.claimed_effect.replace(" ", "")


def _verified(claims) -> list:
    order = {"ASSERTIVE": 0, "SUGGESTIVE": 1, "UNCERTAIN": 2}
    seen, picked = set(), []
    for claim in sorted(claims, key=lambda c: order.get(c.intensity, 3)):
        key = (claim.ingredient, claim.claimed_effect.replace(" ", ""))
        if key not in seen:
            seen.add(key)
            picked.append(claim)
    return picked[:MAX_VERIFIED_CLAIMS]


def build_extractor(kind: str, model_name: str):
    if kind == "rule":
        return RuleBasedClaimExtractor()
    from healthcheck.pipeline.step3_llm import LlmClaimExtractor

    if model_name == "fake":
        from healthcheck.adapters.fake.extraction_model import FakeExtractionModel

        return LlmClaimExtractor(FakeExtractionModel())
    if model_name.startswith("liner"):
        from healthcheck.adapters.liner.extraction_model import LinerExtractionModel
        from healthcheck.config import load_settings

        effort = model_name.split(":", 1)[1] if ":" in model_name else "low"
        key = load_settings().liner_api_key
        if not key:
            raise SystemExit("LINER_API_KEY가 없습니다.")
        return LlmClaimExtractor(CachedModel(LinerExtractionModel(key, reasoning_effort=effort)))
    raise SystemExit(f"아직 연결하지 않은 모델입니다: {model_name}")


class CachedModel:
    """측정용 모델 응답 저장. 검사·순서 규칙만 바꿔 다시 잴 때 같은 응답으로 돈을 쓰지 않고 잰다.

    저장 위치는 data/extraction_cache/(git에 올리지 않음). 지시문을 바꾸면 키가 달라져 새로 부른다.
    """

    def __init__(self, inner) -> None:
        import hashlib

        self._inner = inner
        self._hash = hashlib.sha256
        self.name = inner.name
        self.usage = getattr(inner, "usage", None)
        self._dir = ROOT / "data" / "extraction_cache"
        self._dir.mkdir(parents=True, exist_ok=True)

    async def extract_json(self, *, instructions: str, document: str, schema: dict) -> dict:
        import os

        # 같은 입력을 여러 번 부른 결과를 따로 모을 때 MEASURE_SAMPLE을 바꾼다(모델 결과가 매번 달라 흔들림을 재기 위해).
        sample = os.environ.get("MEASURE_SAMPLE", "0")
        key = self._hash(f"{self.name}\n{sample}\n{instructions}\n{document}".encode()).hexdigest()[:32]
        path = self._dir / f"{key}.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        result = await self._inner.extract_json(instructions=instructions, document=document, schema=schema)
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result


async def measure(extractor, verbose: bool) -> Counter:
    effect_targets = load_patterns().effect_targets
    evaluation = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
    contents = json.loads(CONTENTS_PATH.read_text(encoding="utf-8"))
    total: Counter = Counter()

    # 언어 모델은 글 여러 건을 동시에 보낸다(Liner Model API 초당 10건 제한 안).
    semaphore = asyncio.Semaphore(4)

    async def extract(case):
        text = contents[case["content_key"]]["text"]
        aextract = getattr(extractor, "aextract", None)
        async with semaphore:
            return await aextract(text) if aextract else extractor.extract(text)

    claim_sets = await asyncio.gather(*(extract(case) for case in evaluation["cases"]))
    for case, claim_set in zip(evaluation["cases"], claim_sets):
        claims = list(claim_set.claims)
        verified = _verified(claims)

        gold = case["claims"]
        gold_dict = [g for g in gold if g.get("in_dict")]
        core = [g for g in gold_dict if g.get("core")]
        hit = [g for g in gold_dict if any(_matches(g, c, effect_targets) for c in claims)]
        core_hit = [g for g in core if any(_matches(g, c, effect_targets) for c in claims)]
        core_verified = [g for g in core if any(_matches(g, c, effect_targets) for c in verified)]
        correct = [c for c in claims if any(_matches(g, c, effect_targets) for g in gold)]

        total.update(
            gold=len(gold), gold_dict=len(gold_dict), gold_outside_dict=len(gold) - len(gold_dict),
            hit=len(hit), core=len(core), core_hit=len(core_hit), core_verified=len(core_verified),
            extracted=len(claims), correct=len(correct),
            cases_with_core=int(bool(core)), cases_core_verified=int(bool(core_verified)),
        )
        for flag, value in (
            ("disease_cure", claim_set.disease_cure_expression),
            ("prohibited_ad", claim_set.prohibited_ad_expression),
            ("replace_medicine", not claim_set.in_scope),
        ):
            expected = bool(case["flags"].get(flag))
            total[f"{flag}_{'tp' if expected and value else 'fn' if expected else 'fp' if value else 'tn'}"] += 1

        if verbose:
            missed = [f"{g['ingredient']}/{g['effect']}" for g in core if g not in core_hit]
            wrong = [f"{c.ingredient}/{c.claimed_effect}" for c in claims if c not in correct]
            print(f"{case['id']:8} 핵심 {len(core_hit)}/{len(core)} 뽑음 {len(claims)} 맞음 {len(correct)} "
                  f"놓친 핵심 {missed[:3]} 틀린 추출 {wrong[:3]}")
    return total


def _ratio(a: int, b: int) -> str:
    return f"{a}/{b} ({a / b:.0%})" if b else "-"


def main() -> int:
    parser = argparse.ArgumentParser(description="주장 추출 정확도를 잰다.")
    parser.add_argument("--extractor", choices=["rule", "llm"], default="rule")
    parser.add_argument("--model", default="fake", help="--extractor llm일 때 모델 (fake, liner:low, liner:medium)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.disable(logging.WARNING)

    extractor = build_extractor(args.extractor, args.model)
    started = time.monotonic()
    total = asyncio.run(measure(extractor, args.verbose))
    elapsed = time.monotonic() - started
    usage = getattr(getattr(extractor, "_model", None), "usage", None)
    label = args.extractor if args.extractor == "rule" else f"llm/{args.model}"
    print(f"\n추출기 {label} · 평가셋 {EVAL_PATH.relative_to(ROOT)}")
    print(f"  핵심 재현율   {_ratio(total['core_hit'], total['core'])}")
    print(f"  전체 재현율   {_ratio(total['hit'], total['gold_dict'])}  (사전 밖 성분 주장 {total['gold_outside_dict']}건 제외)")
    print(f"  정밀도       {_ratio(total['correct'], total['extracted'])}")
    print(f"  핵심 검증     {_ratio(total['core_verified'], total['core'])}, 글 기준 {_ratio(total['cases_core_verified'], total['cases_with_core'])}")
    for flag, name in (("disease_cure", "질병 치료"), ("prohibited_ad", "금지 광고"), ("replace_medicine", "약 대체")):
        tp, fn, fp = total[f"{flag}_tp"], total[f"{flag}_fn"], total[f"{flag}_fp"]
        print(f"  {name:6} 적중 {tp} 놓침 {fn} 오탐 {fp}")
    print(f"  시간 {elapsed:.0f}초")
    if usage:
        # Liner Model API 단가(2026-09-17 문서): 입력 100만 토큰당 $1, 출력(추론 포함) $6
        cost = usage["prompt_tokens"] / 1e6 * 1 + usage["completion_tokens"] / 1e6 * 6
        print(f"  호출 {usage['calls']}회 · 입력 {usage['prompt_tokens']:,} · 출력 {usage['completion_tokens']:,} 토큰 · 약 ${cost:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
