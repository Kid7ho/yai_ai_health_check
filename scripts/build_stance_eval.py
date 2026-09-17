"""논문 입장 분류 평가셋의 (주장, 논문) 쌍을 만든다.

분류 시트 사례(labeling/cases.json)에 담긴 주장과 논문을 한 쌍씩 풀어 tests/stance_eval/pairs.json에 쓴다.
정답(labels.json)은 따로 둔다. 팀원 분류가 모이면 그 값으로 바꾼다.

    uv run python scripts/build_stance_eval.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from healthcheck.adapters.liner.academic import query_terms_of
from healthcheck.domain.types import Claim

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "labeling" / "cases.json"
OUT = ROOT / "tests" / "stance_eval" / "pairs.json"


def main() -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    pairs: dict[str, dict] = {}
    for case in cases:
        for claim in case["claims"]:
            probe = Claim(
                ingredient=claim["ingredient"],
                claimed_effect=claim["effect"],
                intensity=claim.get("intensity") or "SUGGESTIVE",
                span=(0, 0),
            )
            ingredient_en, effect_en = query_terms_of(probe)
            for paper in claim["papers"]:
                key = f"{claim['ingredient']}|{effect_en or claim['effect']}|{paper['url']}"
                pair_id = "st-" + hashlib.sha1(key.encode()).hexdigest()[:10]
                pairs.setdefault(
                    pair_id,
                    {
                        "id": pair_id,
                        "ingredient": claim["ingredient"],
                        "ingredient_en": ingredient_en,
                        "effect": claim["effect"],
                        "effect_en": effect_en,
                        "statement": claim.get("statement") or "",
                        "title": paper["title"],
                        "abstract": paper.get("abstract") or "",
                        "url": paper["url"],
                    },
                )
    ordered = sorted(pairs.values(), key=lambda p: p["id"])
    OUT.write_text(json.dumps(ordered, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"쌍 {len(ordered)}건 → {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
