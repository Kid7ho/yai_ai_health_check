"""키 없이 LLM 주장 추출 경로를 끝까지 돌리는 가짜 모델.

기본 동작은 규칙 기반 추출이 찾은 주장을 모델 출력 형식으로 옮겨 돌려준다.
테스트는 responder로 원하는 출력(원문에 없는 구절, 기업 서술, 인용 표시 등)을 직접 넣는다.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from healthcheck.ports.extraction_model import ExtractionModelError


class FakeExtractionModel:
    name = "fake"

    def __init__(self, responder: Callable[[str], dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self._responder = responder
        self._fail = fail
        self.calls: list[str] = []

    async def extract_json(self, *, instructions: str, document: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(document)
        if self._fail:
            raise ExtractionModelError("가짜 모델 실패")
        if self._responder is not None:
            return self._responder(document)
        return _from_rules(document)


def _from_rules(document: str) -> dict[str, Any]:
    from healthcheck.pipeline.step3_claims import RuleBasedClaimExtractor

    claim_set = RuleBasedClaimExtractor().extract(document)
    claims = []
    for claim in claim_set.claims:
        quote = claim.statement or ""
        raw = claim.raw_ingredient or claim.ingredient
        if not quote or raw not in quote:
            continue
        claims.append(
            {
                "quote": quote.rstrip("…"),
                "ingredient_text": raw,
                "ingredient_en": "",
                "effect_text": claim.claimed_effect if claim.claimed_effect in quote else quote,
                "effect_target": "기타",
                "claim_kind": "효능",
                "voice": "주장",
            }
        )
    return {"claims": claims, "expressions": {"medicine_replacement": [], "ad_like": []}}
