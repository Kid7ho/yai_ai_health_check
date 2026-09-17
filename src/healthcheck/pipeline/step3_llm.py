"""③ 주장 구조화 — 언어 모델 추출.

실제 링크 45건 점검(2026-09-17)에서 규칙 기반 추출의 핵심 주장 재현율이 40%였다. 효능을 나열한
문장, 문장 부호 없는 자막, 사전 밖 성분, 대명사를 규칙으로 풀기 어려웠다.

언어 모델은 **원문 구절을 옮겨 적기만** 한다. 지키는 선은 셋이다.
  1. 판정은 계속 규칙표가 한다. 모델은 라벨·평가·요약을 내지 않는다
  2. 모델 출력은 ClaimValidator가 원문과 대조해 거른다. 원문에 없는 구절, 업체·인물 서술,
     인용·부정 문장은 판정에 닿지 않는다
  3. 질병 치료·금지 광고·약 대체 여부는 규칙 기반 표현 목록으로 계산한다. 모델이 인용·부정으로
     표시한 문장의 표현만 뺀다(광고를 비판하며 인용한 글이 경고를 받던 문제)

모델 호출이 실패하면 규칙 기반 추출로 대신한다. 모델 출력과 버린 항목은 로그에 남긴다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from healthcheck.domain.types import ClaimSet
from healthcheck.pipeline.claim_validator import VALIDATOR_VERSION, ClaimValidator, ValidationResult, normalize
from healthcheck.pipeline.step3_claims import (
    RuleBasedClaimExtractor,
    _contains_any,
    _contains_refusal,
    _find_brands,
    _find_roles,
    _sentences,
    load_patterns,
)
from healthcheck.ports.extraction_model import ExtractionModel, ExtractionModelError

logger = logging.getLogger(__name__)

RULES_DIR = Path(__file__).resolve().parents[3] / "rules"
DEFAULT_PROMPT_PATH = RULES_DIR / "claim_extraction_prompt.yaml"

# 한 번에 모델에 넘기는 본문 길이. 긴 자막은 겹치게 나눠 보낸다.
CHUNK_CHARS = 12_000
CHUNK_OVERLAP = 500
# 동시에 보내는 조각 수. 공급자 호출 제한을 넘지 않게 작게 둔다.
MAX_PARALLEL_CHUNKS = 2
# 판정 단계로 넘기는 주장 수의 상한. 검증은 이 중 최대 3건만 한다(orchestrator).
MAX_CLAIMS = 12

# 개인정보. 모델에 보내기 전에 같은 길이의 '*'로 가린다. 길이를 지켜야 주장 위치가 원문과 맞는다.
_PHONE = re.compile(r"(?<!\d)0\d{1,2}[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

# 출력 형식(지시문 p4). 판정에 쓰지 않던 칸(영문 이름, 효능 문구, 주장 종류, 광고 표현)을 뺐다.
# 이전 형식은 응답 JSON이 중앙값 2,750자였고 그중 약 40%가 이 칸이었다. 출력이 길수록 모델 응답이 늦다.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string"},
                    "ingredient_text": {"type": "string"},
                    "effect_target": {"type": "string"},
                    "voice": {"type": "string", "enum": ["주장", "인용", "부정", "질문"]},
                },
                "required": ["quote", "ingredient_text", "effect_target", "voice"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["claims"],
    # 출력 형식 강제(strict)는 정의하지 않은 칸을 허용하지 않아야 한다. 모델이 평가·요약 칸을 덧붙일 수 없다.
    "additionalProperties": False,
}


FOODS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"foods": {"type": "array", "items": {"type": "string"}}},
    "required": ["foods"],
    "additionalProperties": False,
}


@lru_cache(maxsize=2)
def load_two_stage_prompts(path: Path | str = DEFAULT_PROMPT_PATH) -> tuple[str, str]:
    """(1단계 식품 목록 지시문, 2단계에 붙일 대상 식품 지시 틀)."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return str(raw.get("식품_목록_지시문") or ""), str(raw.get("대상_식품_지시") or "")


@lru_cache(maxsize=2)
def load_prompt(path: Path | str = DEFAULT_PROMPT_PATH) -> tuple[str, str]:
    """(지시문 전체, 지시문 버전). 효능 대상 목록과 예시를 채워 넣는다."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    targets = ", ".join(sorted(load_patterns().effect_targets)) + ", 기타"
    instructions = str(raw.get("지시문") or "").replace("{효능_대상_목록}", targets)
    examples = raw.get("예시") or []
    if examples:
        rendered = yaml.safe_dump(examples, allow_unicode=True, sort_keys=False, width=200)
        instructions += "\n예시 (본문 → 출력)\n" + rendered
    return instructions, str(raw.get("version") or "p0")


def mask_personal_info(text: str) -> str:
    """전화번호·이메일을 같은 길이의 '*'로 가린다."""
    text = _PHONE.sub(lambda m: "*" * len(m.group()), text)
    return _EMAIL.sub(lambda m: "*" * len(m.group()), text)


def chunks(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[tuple[int, str]]:
    """(시작 위치, 조각). 조각 경계는 가능하면 문장 끝에 맞춘다."""
    if len(text) <= size:
        return [(0, text)]
    parts: list[tuple[int, str]] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = max(text.rfind(".", start, end), text.rfind("\n", start, end), text.rfind("다 ", start, end))
            if cut > start + size // 2:
                end = cut + 1
        parts.append((start, text[start:end]))
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return parts


class LlmClaimExtractor:
    """ClaimExtractor 인터페이스. 비동기 추출(aextract)을 함께 둔다."""

    def __init__(
        self,
        model: ExtractionModel,
        *,
        fallback: RuleBasedClaimExtractor | None = None,
        prompt_path: Path | str = DEFAULT_PROMPT_PATH,
        merge_rule_claims: bool = False,
        two_stage: bool = False,
        samples: int = 1,
    ) -> None:
        # 같은 요청을 동시에 여러 번 보내 주장을 합친다. Liner는 temperature·seed를 적용하지 않아 호출마다
        # 뽑는 주장이 달라진다. 평가셋에서 1회 66~74%, 3회 합치기 81%였다(2026-09-17). 동시에 보내 대기 시간은 거의 같다.
        self._samples = max(1, samples)
        # 두 단계 추출: 식품 목록을 먼저 뽑고 그 식품마다 주장을 빠짐없이 뽑게 한다. 호출이 조각마다 두 번이다.
        self._two_stage = two_stage
        self._foods_instructions, self._target_hint = load_two_stage_prompts(prompt_path)
        self._model = model
        # 규칙 기반이 찾은 주장을 뒤에 합친다. 평가셋에서 Liner 단독보다 핵심 재현율이 높았다(66~73%).
        # 모델 호출마다 결과가 흔들리므로 규칙이 확실히 잡는 짧은 주장을 받쳐 준다.
        self._merge_rule_claims = merge_rule_claims
        self._fallback = fallback or RuleBasedClaimExtractor()
        self._patterns = load_patterns()
        self._validator = ClaimValidator(self._patterns)
        self._instructions, self._prompt_version = load_prompt(prompt_path)
        self.last_validation: ValidationResult | None = None

    @property
    def cache_namespace(self) -> str:
        """추출 캐시 키 앞에 붙는 값. 모델·지시문·검사 규칙이 바뀌면 이전 추출을 쓰지 않는다."""
        merged = ("+rule" if self._merge_rule_claims else "") + ("+2stage" if self._two_stage else "") + (f"x{self._samples}" if self._samples > 1 else "")
        return f"llm:{self._model.name}{merged}:{self._prompt_version}:{VALIDATOR_VERSION}"

    def extract(self, text: str) -> ClaimSet:
        """동기 호출 경로는 규칙 기반으로 대신한다. 파이프라인은 aextract를 쓴다."""
        return self._fallback.extract(text)

    async def aextract(self, text: str) -> ClaimSet:
        text = text or ""
        masked = mask_personal_info(text)
        try:
            validation = await self._extract_validated(masked)
        except ExtractionModelError as error:
            logger.warning("언어 모델 주장 추출 실패, 규칙 기반으로 대신한다 model=%s: %s", self._model.name, error)
            return self._fallback.extract(text)

        self.last_validation = validation
        claims = _rank([v for v in validation.claims], masked)[:MAX_CLAIMS]
        logger.info(
            "언어 모델 주장 추출 model=%s prompt=%s 통과=%d 제외=%d 이유=%s",
            self._model.name,
            self._prompt_version,
            len(claims),
            len(validation.rejected),
            sorted({r.reason for r in validation.rejected}),
        )
        disease_cure, prohibited_ad, out_of_scope = self._expression_flags(masked, validation)
        final = [v.claim for v in claims]
        if self._merge_rule_claims:
            seen = {(c.ingredient, c.claimed_effect.replace(" ", "")) for c in final}
            seen_ingredients_targets = {(v.claim.ingredient, v.effect_target) for v in claims}
            for rule_claim in self._fallback.extract(text).claims:
                key = (rule_claim.ingredient, rule_claim.claimed_effect.replace(" ", ""))
                targets = [t for t in self._patterns.effect_targets if t in rule_claim.claimed_effect]
                if key in seen or any((rule_claim.ingredient, t) in seen_ingredients_targets for t in targets):
                    continue
                seen.add(key)
                final.append(rule_claim)
        return ClaimSet(
            claims=tuple(final[:MAX_CLAIMS]),
            in_scope=not out_of_scope,
            disease_cure_expression=disease_cure,
            prohibited_ad_expression=prohibited_ad,
            extraction_failed=not final,
            # 모델이 인용·부정으로 표시한 식품 주장이라도 음식 이야기는 있는 글이다.
            food_mentioned=bool(final) or bool(validation.non_asserted_quotes) or self._fallback.extract(text).food_mentioned,
            brands=_find_brands(text, self._patterns.brand_lookup),
            authority_roles=_find_roles(text, self._patterns.roles, self._patterns.role_exclusions),
        )

    async def _extract_validated(self, text: str) -> ValidationResult:
        semaphore = asyncio.Semaphore(MAX_PARALLEL_CHUNKS)

        async def one(offset: int, part: str) -> ValidationResult:
            instructions = self._instructions
            async with semaphore:
                if self._two_stage and self._foods_instructions:
                    found = await self._model.extract_json(
                        instructions=self._foods_instructions,
                        document=f"<본문>\n{part.replace('</본문>', '</ 본문>')}\n</본문>",
                        schema=FOODS_SCHEMA,
                        quick=True,
                    )
                    source = normalize(part)
                    # 원문에 있는 이름만 쓴다. 모델이 지어낸 이름을 2단계 지시에 넣지 않는다.
                    foods = [f.strip() for f in (found.get("foods") or []) if isinstance(f, str) and f.strip() and normalize(f) in source]
                    if foods:
                        instructions = instructions + "\n" + self._target_hint.replace("{식품_목록}", ", ".join(dict.fromkeys(foods))[:500])
                document = f"<본문>\n{part.replace('</본문>', '</ 본문>')}\n</본문>"
                results = await asyncio.gather(
                    *(
                        self._model.extract_json(instructions=instructions, document=document, schema=SCHEMA, sample=i)
                        for i in range(self._samples)
                    ),
                    return_exceptions=True,
                )
            outputs = [r for r in results if isinstance(r, dict)]
            if not outputs:
                error = next((r for r in results if isinstance(r, ExtractionModelError)), None)
                raise error or ExtractionModelError("모든 호출이 실패했습니다")
            # 여러 응답의 주장을 합친다. 중복은 검사기가 (성분, 효능 대상)으로 합친다.
            output = {"claims": [claim for out in outputs for claim in (out.get("claims") or [])]}
            return self._validator.validate(output, part, offset=offset)

        results = await asyncio.gather(*(one(offset, part) for offset, part in chunks(text)))
        merged = ValidationResult()
        seen: set[tuple[str, str]] = set()
        for result in results:
            for validated in result.claims:
                key = (validated.claim.ingredient, validated.effect_target if validated.effect_target != "기타" else validated.normalized_quote)
                if key not in seen:
                    seen.add(key)
                    merged.claims.append(validated)
            merged.non_asserted_quotes.extend(result.non_asserted_quotes)
            merged.medicine_replacement.extend(result.medicine_replacement)
            merged.ad_like.extend(result.ad_like)
            merged.rejected.extend(result.rejected)
        return merged

    def _expression_flags(self, text: str, validation: ValidationResult) -> tuple[bool, bool, bool]:
        """질병 치료·금지 광고·약 대체 여부를 규칙 기반 표현 목록으로 계산한다.

        모델이 인용·부정으로 표시한 구절이 들어 있는 문장은 세지 않는다. 판단은 여전히 표현 목록이 하고,
        모델은 '이 문장은 남의 말을 옮긴 것'이라는 표시만 준다.
        """
        patterns = self._patterns
        excluded = [normalize(q) for q in validation.non_asserted_quotes if q]
        rule = RuleBasedClaimExtractor()

        disease_cure = prohibited_ad = out_of_scope = False
        for sentence, _ in _sentences(text):
            normalized_sentence = normalize(sentence)
            if any(q and (q in normalized_sentence or normalized_sentence in q) for q in excluded):
                continue
            if _contains_any(sentence, patterns.negation):
                continue
            if rule._is_disease_cure(sentence):
                disease_cure = True
            if _contains_any(sentence, patterns.prohibited_ad):
                prohibited_ad = True
            if _contains_refusal(sentence, patterns.out_of_scope_immediate, patterns.out_of_scope_excluded_words):
                out_of_scope = True
        return disease_cure, prohibited_ad, out_of_scope


def _rank(claims: list, text: str) -> list:
    """판정 단계로 넘길 순서. 모델이 아니라 코드가 정한다.

    질병 이름이 든 주장 → 제목(첫 줄)에 나온 주장 → 같은 성분이 여러 번 나온 주장 → 앞쪽 주장 순이다.
    orchestrator는 이 순서를 유지한 채 단정 표현을 먼저 골라 최대 3건을 검증한다.
    """
    patterns = load_patterns()
    first_line = text.split("\n", 1)[0][:200]
    counts: dict[str, int] = {}
    for validated in claims:
        counts[validated.claim.ingredient] = text.count(validated.claim.raw_ingredient or validated.claim.ingredient)

    def key(validated) -> tuple:
        claim = validated.claim
        statement = claim.statement or ""
        has_disease = _contains_any(statement, patterns.disease_names)
        in_title = claim.span[0] < len(first_line)
        return (not has_disease, not in_title, -counts.get(claim.ingredient, 0), claim.span[0])

    return sorted(claims, key=key)


__all__ = ["LlmClaimExtractor", "SCHEMA", "chunks", "load_prompt", "mask_personal_info"]
