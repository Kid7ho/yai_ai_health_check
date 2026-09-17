#!/usr/bin/env python3
"""논문 입장 분류 정확도를 평가셋(주장·논문 383쌍)으로 잰다.

    .venv/bin/python scripts/measure_stance.py                          # 표현 목록 분류
    .venv/bin/python scripts/measure_stance.py --classifier llm --model liner:low
    .venv/bin/python scripts/measure_stance.py --classifier llm --model gemini --verbose

지표
    정확도        세 값(SUPPORT / REFUTE / NONE) 모두 맞힌 비율. 서비스의 NEUTRAL은 NONE으로 센다
    반박 정밀도    반박이라고 한 것 중 정답도 반박인 비율. 낮으면 믿을 만한 주장이 조심하세요로 떨어진다
    반박 재현율    정답 반박 중 반박이라고 한 비율. 낮으면 반박 논문이 있는 주장을 믿을 만하다고 한다
    뒷받침 정밀도·재현율  같은 방식

정답(tests/stance_eval/labels.json)은 AI가 초록만 보고 표시했고 팀원 검수 전이다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from healthcheck.adapters.liner.academic import classify_stance  # noqa: E402
from healthcheck.domain.types import Paper  # noqa: E402

PAIRS_PATH = ROOT / "tests" / "stance_eval" / "pairs.json"
LABELS_PATH = ROOT / "tests" / "stance_eval" / "labels.json"
VALUES = ("SUPPORT", "REFUTE", "NONE")


def build_classifier(model_name: str, samples: int = 1, verify: str | None = None):
    from healthcheck.config import load_settings
    from healthcheck.pipeline.paper_stance import LlmStanceClassifier
    from measure_extraction import CachedModel

    settings = load_settings()
    if model_name.startswith("liner"):
        from healthcheck.adapters.liner.extraction_model import LinerExtractionModel

        effort = model_name.split(":", 1)[1] if ":" in model_name else "low"
        if not settings.liner_api_key:
            raise SystemExit("LINER_API_KEY가 없습니다")
        verifier = (
            CachedModel(LinerExtractionModel(settings.liner_api_key, reasoning_effort=verify)) if verify else None
        )
        return LlmStanceClassifier(
            CachedModel(LinerExtractionModel(settings.liner_api_key, reasoning_effort=effort)),
            samples=samples,
            verifier=verifier,
        )
    if model_name.startswith("gemini"):
        from healthcheck.adapters.gemini.extraction_model import GeminiExtractionModel

        name = model_name.split(":", 1)[1] if ":" in model_name else settings.gemini_model
        if not settings.gemini_api_key:
            raise SystemExit("GEMINI_API_KEY가 없습니다")
        return LlmStanceClassifier(
            CachedModel(GeminiExtractionModel(settings.gemini_api_key, model=name)), samples=samples
        )
    raise SystemExit(f"모르는 모델: {model_name}")


async def predict_llm(pairs: list[dict], classifier, parallel: int) -> tuple[dict[str, str], list[float]]:
    # 운영과 같이 주장(성분, 효능)마다 논문을 묶어 한 번에 분류한다. 운영의 한 주장당 관련 논문은 최대 10편이다.
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for pair in pairs:
        groups[(pair["ingredient_en"], pair["effect_en"])].append(pair)
    jobs = []
    for (ingredient, effect), members in groups.items():
        for start in range(0, len(members), 10):
            jobs.append((ingredient, effect, members[start : start + 10]))

    predictions: dict[str, str] = {}
    seconds: list[float] = []
    gate = asyncio.Semaphore(parallel)

    async def run(ingredient: str, effect: str, members: list[dict]) -> None:
        papers = tuple(
            Paper(title=m["title"], url=m["url"], description=m["abstract"], stance=_rule(m)) for m in members
        )
        async with gate:
            started = time.monotonic()
            result = await classifier.classify(ingredient=ingredient, effect=effect, papers=papers)
            seconds.append(time.monotonic() - started)
        for member, paper in zip(members, result):
            predictions[member["id"]] = paper.stance

    await asyncio.gather(*(run(*job) for job in jobs))
    return predictions, seconds


def _rule(pair: dict) -> str:
    return classify_stance(pair["title"], pair["abstract"])


def evidence_grade(stances: list[str]) -> str:
    """규칙표 임계값과 같은 기준의 주장 단위 근거 판단. 뒷받침이 반박의 두 배 이상이면 뒷받침 우세."""
    support = stances.count("SUPPORT")
    refute = stances.count("REFUTE")
    if support == 0 and refute == 0:
        return "없음"
    if support >= 1 and support >= 2 * refute:
        return "뒷받침 우세"
    if refute >= support + 2:
        return "반박 우세"
    return "엇갈림"


def report(pairs: list[dict], labels: dict[str, str], predictions: dict[str, str], verbose: bool) -> None:
    confusion: Counter = Counter()
    for pair in pairs:
        gold = labels[pair["id"]]
        predicted = predictions[pair["id"]]
        predicted = "NONE" if predicted == "NEUTRAL" else predicted
        confusion[(gold, predicted)] += 1
        if verbose and gold != predicted and "REFUTE" in (gold, predicted):
            print(f"  [{gold}→{predicted}] {pair['ingredient_en']} / {pair['effect_en']} | {pair['title'][:90]}")

    total = sum(confusion.values())
    correct = sum(confusion[(v, v)] for v in VALUES)
    print(f"  정확도       {correct}/{total} ({correct / total:.0%})")
    for value, label in (("REFUTE", "반박"), ("SUPPORT", "뒷받침")):
        said = sum(confusion[(g, value)] for g in VALUES)
        gold = sum(confusion[(value, p)] for p in VALUES)
        hit = confusion[(value, value)]
        precision = f"{hit}/{said} ({hit / said:.0%})" if said else "0/0"
        recall = f"{hit}/{gold} ({hit / gold:.0%})" if gold else "0/0"
        print(f"  {label} 정밀도 {precision} · 재현율 {recall}")
    grades: Counter = Counter()
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for pair in pairs:
        groups[(pair["ingredient_en"], pair["effect_en"])].append(pair)
    for members in groups.values():
        gold_grade = evidence_grade([labels[m["id"]] for m in members])
        predicted_grade = evidence_grade([predictions[m["id"]] for m in members])
        grades[gold_grade == predicted_grade] += 1
        if verbose and gold_grade != predicted_grade:
            print(f"  주장 {members[0]['ingredient_en']} / {members[0]['effect_en']}: 정답 {gold_grade} → 분류 {predicted_grade}")
    print(f"  주장 근거 판단 {grades[True]}/{len(groups)} ({grades[True] / len(groups):.0%})  (뒷받침 우세·반박 우세·엇갈림·없음)")
    print("  정답\\분류   " + "  ".join(f"{v:>8}" for v in VALUES))
    for gold in VALUES:
        print(f"  {gold:<10} " + "  ".join(f"{confusion[(gold, p)]:>8}" for p in VALUES))


def main() -> int:
    parser = argparse.ArgumentParser(description="논문 입장 분류 정확도를 잰다.")
    parser.add_argument("--classifier", choices=("rule", "llm"), default="rule")
    parser.add_argument("--model", default="liner:low", help="liner:low, liner:none, gemini, gemini:<모델명>")
    parser.add_argument("--samples", type=int, default=1, help="같은 묶음을 동시에 보내는 횟수(과반 투표)")
    parser.add_argument("--parallel", type=int, default=3, help="동시에 분류하는 주장 수")
    parser.add_argument("--verify", default=None, help="반박 후보를 다시 확인하는 Liner 추론 수준(예: low). 없으면 확인하지 않는다")
    parser.add_argument("--split", choices=("all", "dev", "test"), default="all", help="평가셋 절반(dev: 조정용, test: 확인용)")
    parser.add_argument("--verbose", action="store_true", help="반박이 걸린 오분류를 보여 준다")
    args = parser.parse_args()

    pairs = json.loads(PAIRS_PATH.read_text(encoding="utf-8"))
    if args.split != "all":
        # 지시문·검사를 고칠 때는 dev만 보고, test는 결과 확인에만 쓴다. 정답이 AI 표시라 그 버릇에 맞추지 않기 위해서다.
        want = 0 if args.split == "dev" else 1
        pairs = [pair for pair in pairs if int(pair["id"][3:], 16) % 2 == want]
    labels = json.loads(LABELS_PATH.read_text(encoding="utf-8"))["labels"]

    if args.classifier == "rule":
        print("분류기 표현 목록(rules/stance_patterns.yaml)")
        predictions = {pair["id"]: _rule(pair) for pair in pairs}
        report(pairs, labels, predictions, args.verbose)
        return 0

    classifier = build_classifier(args.model, args.samples, args.verify)
    print(f"분류기 {classifier.name}")
    started = time.monotonic()
    predictions, seconds = asyncio.run(predict_llm(pairs, classifier, args.parallel))
    report(pairs, labels, predictions, args.verbose)
    usage = dict(getattr(getattr(classifier, "_model", None), "usage", None) or {})
    verify_usage = getattr(getattr(classifier, "_verifier", None), "usage", None) or {}
    for key, value in verify_usage.items():
        usage[key] = usage.get(key, 0) + value
    print(
        f"  시간 {time.monotonic() - started:.0f}초 · 주장 1건(논문 최대 10편) 중앙값 {statistics.median(seconds):.1f}초, "
        f"최대 {max(seconds):.1f}초 (저장된 응답은 0초)"
    )
    if usage.get("calls"):
        print(f"  호출 {usage['calls']}회 · 입력 {usage['prompt_tokens']:,} · 출력 {usage['completion_tokens']:,} 토큰")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
