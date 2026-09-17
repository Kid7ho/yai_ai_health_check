"""언어 모델이 옮겨 적은 주장을 원문과 대조해 거른다.

모델 출력은 믿지 않는다. 여기서 통과한 것만 ④ 검증과 ⑤ 판정에 닿는다.

거르는 기준
  1. quote가 원문에 글자 그대로 있어야 한다. 띄어쓰기·따옴표 모양 차이만 허용한다
  2. ingredient_text와 effect_text가 quote 안에 있어야 한다
  3. quote에 브랜드 이름, 사람 이름+호칭, 업체를 가리키는 말이 있으면 버린다.
     특정 업체나 인물에 대한 서술이 주장으로 나가는 일을 구조적으로 막는다
  4. voice가 "주장"이 아니면(인용·부정·질문) 판정에 쓰지 않는다. 버린 이유와 함께 남긴다
  5. 같은 (성분, 효능) 주장은 하나로 합친다

모델의 claim_kind와 effect_target은 분류일 뿐 판정이 아니다. 표현 강도와 질병 치료·금지 광고
여부는 규칙 기반 추출의 표현 목록으로 원문 구절에서 다시 계산한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from healthcheck.domain.types import Claim

VALIDATOR_VERSION = "v3"
QUOTE_MAX = 120
# 성분 이름이 구절 바로 앞 문맥에 있으면 인정하는 거리(정규화 글자 수). "루테인은 … 시력 저하를 막아줘요"처럼
# 모델이 효능 부분만 옮기면 성분 이름이 구절 밖에 남아 올바른 주장이 버려졌다(운영 실측 10건 중 9건).
INGREDIENT_LOOKBACK = 100
STATEMENT_MAX = 160
VOICES = ("주장", "인용", "부정", "질문")
KINDS = ("효능", "질병치료", "질병예방")

# 사람 이름 + 호칭. "김희진 원장", "오창훈 원장님". "의사들이", "약사가"처럼 이름 없는 역할은 걸리지 않게
# 이름(2~3자)과 호칭 사이에 띄어쓰기가 있고 이름 앞이 한글이 아닐 때만 본다.
# '대표'·'선생'은 "대표 성분", "선생님들" 같은 일반어와 섞여 넣지 않았다. 호칭 뒤에는 조사·띄어쓰기·문장부호만 온다.
_PERSON = re.compile(r"(?<![가-힣])[가-힣]{2,3} (?:원장|교수|박사|기자)(?:님)?(?=[은는이가의께도과와]|\s|[,.!?)]|$)")
# 업체·판매를 가리키는 말. 주장 구절에 섞이면 특정 업체 서술일 수 있어 버린다.
_BUSINESS = re.compile(r"회사|업체|기업|상사|판매자|판매처|쇼핑몰|㈜|\(주\)|주식회사|대리점")
# 이름이 없는 대상. "이 음료가 암을 없앤다"는 무엇에 대한 주장인지 검증할 수 없다.
_GENERIC_INGREDIENT = re.compile(r"^(?:이|그|저)?\s*(?:것|거|걸|이것|이거|음료|음식|식품|제품|영양제|약|성분|재료|이런\s*것|이런\s*걸)$")
_QUOTE_CHARS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "「": "'", "」": "'"})


def normalize(text: str) -> str:
    """대조용 표기. 띄어쓰기를 없애고 따옴표 모양을 맞춘다."""
    return re.sub(r"\s+", "", (text or "").translate(_QUOTE_CHARS))


@dataclass(frozen=True, slots=True)
class Rejection:
    quote: str
    reason: str


@dataclass(frozen=True, slots=True)
class ValidatedClaim:
    claim: Claim
    kind: str
    effect_target: str
    # 원문 대조용. 판정 문장 필터(인용 문장 제외)에 쓴다.
    normalized_quote: str


@dataclass(slots=True)
class ValidationResult:
    claims: list[ValidatedClaim] = field(default_factory=list)
    # 인용·부정·질문으로 표시되어 판정에서 뺀 구절. 원문에서 이 구절이 든 문장의 질병 치료·광고 표현을 세지 않는다
    non_asserted_quotes: list[str] = field(default_factory=list)
    medicine_replacement: list[str] = field(default_factory=list)
    ad_like: list[str] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)


class ClaimValidator:
    def __init__(self, patterns) -> None:
        # step3_claims._Patterns. 순환 import를 피하려고 타입을 적지 않는다.
        self._patterns = patterns
        self._brands = tuple(sorted(patterns.brand_lookup, key=len, reverse=True))

    def validate(self, output: dict[str, Any], source: str, *, offset: int = 0) -> ValidationResult:
        """모델 출력 하나(본문 조각 하나)를 거른다. offset은 조각의 원문 시작 위치다."""
        result = ValidationResult()
        normalized_source, index_map = _normalized_with_index(source)

        raw_claims = output.get("claims") if isinstance(output, dict) else None
        if not isinstance(raw_claims, list):
            result.rejected.append(Rejection("", "claims가 배열이 아님"))
            raw_claims = []

        seen: set[tuple[str, str]] = set()
        for item in raw_claims:
            if not isinstance(item, dict):
                result.rejected.append(Rejection("", "항목이 객체가 아님"))
                continue
            quote = _text(item.get("quote"))[:QUOTE_MAX]
            ingredient_text = _text(item.get("ingredient_text"))
            effect_text = _text(item.get("effect_text"))
            voice = _text(item.get("voice"))
            kind = _text(item.get("claim_kind"))

            if not quote or not ingredient_text or not effect_text:
                result.rejected.append(Rejection(quote, "빈 칸"))
                continue
            position = normalized_source.find(normalize(quote))
            if position < 0:
                result.rejected.append(Rejection(quote, "원문에 없는 구절"))
                continue
            normalized_quote = normalize(quote)
            normalized_ingredient = normalize(ingredient_text)
            if normalize(effect_text) not in normalized_quote:
                result.rejected.append(Rejection(quote, "효능이 구절 안에 없음"))
                continue
            if normalized_ingredient not in normalized_quote:
                # 성분 이름이 바로 앞 문맥에 있으면 그 자리부터를 주장 구절로 넓힌다.
                window_start = max(0, position - INGREDIENT_LOOKBACK)
                found = normalized_source.rfind(normalized_ingredient, window_start, position)
                if found < 0 or (position + len(normalized_quote)) - found > STATEMENT_MAX:
                    result.rejected.append(Rejection(quote, "성분이 구절과 앞 문맥에 없음"))
                    continue
                normalized_quote = normalized_source[found : position + len(normalized_quote)]
                position = found
            if voice not in VOICES or kind not in KINDS:
                result.rejected.append(Rejection(quote, "분류 값이 목록 밖"))
                continue
            if voice != "주장":
                result.non_asserted_quotes.append(quote)
                result.rejected.append(Rejection(quote, f"voice={voice}"))
                continue
            if _GENERIC_INGREDIENT.match(ingredient_text):
                result.rejected.append(Rejection(quote, "이름 없는 대상"))
                continue
            display = source[index_map[position] : index_map[position + len(normalized_quote) - 1] + 1]
            if self._mentions_business_or_person(display):
                result.rejected.append(Rejection(quote, "업체·브랜드·인물 서술 포함"))
                continue

            ingredient = self._standard_ingredient(ingredient_text)
            target = _text(item.get("effect_target")) or "기타"
            claimed_effect = effect_text
            # 효능 대상 단어가 효능 표현에 없으면 식약처 일치도와 학술 검색어를 만들 수 없다.
            # 모델이 목록에서 고른 효능 대상을 괄호로 붙인다. 목록 밖 값은 붙이지 않는다.
            if target != "기타" and target in self._patterns.effect_targets and target not in claimed_effect:
                claimed_effect = f"{claimed_effect} ({target})"
            key = (ingredient, target if target != "기타" else normalize(effect_text))
            if key in seen:
                result.rejected.append(Rejection(quote, "중복"))
                continue
            seen.add(key)

            start = offset + index_map[position]
            end_index = position + len(normalized_quote) - 1
            end = offset + index_map[end_index] + 1
            intensity = _intensity(quote, self._patterns)
            ingredient_en = _text(item.get("ingredient_en"))[:60]
            result.claims.append(
                ValidatedClaim(
                    claim=Claim(
                        ingredient=ingredient,
                        claimed_effect=claimed_effect,
                        intensity=intensity,
                        span=(start, end),
                        raw_ingredient=ingredient_text if ingredient_text != ingredient else None,
                        statement=source[index_map[position] : index_map[end_index] + 1],
                        ingredient_en=ingredient_en or None,
                    ),
                    kind=kind,
                    effect_target=target,
                    normalized_quote=normalized_quote,
                )
            )

        expressions = output.get("expressions") if isinstance(output, dict) else None
        expressions = expressions if isinstance(expressions, dict) else {}
        for name in ("medicine_replacement", "ad_like"):
            for quote in expressions.get(name) or ():
                quote = _text(quote)[:QUOTE_MAX]
                if quote and normalize(quote) in normalized_source:
                    getattr(result, name).append(quote)
                elif quote:
                    result.rejected.append(Rejection(quote, f"{name}: 원문에 없는 구절"))
        return result

    def _mentions_business_or_person(self, quote: str) -> bool:
        if _PERSON.search(quote) or _BUSINESS.search(quote):
            return True
        for brand in self._brands:
            if re.fullmatch(r"[A-Za-z0-9]+", brand):
                # "hy", "GNC" 같은 영문 브랜드는 다른 영단어 안에서 잡히지 않게 단어 경계로 본다.
                if re.search(rf"(?<![A-Za-z0-9]){re.escape(brand)}(?![A-Za-z0-9])", quote):
                    return True
            elif brand in quote:
                return True
        return False

    def _standard_ingredient(self, ingredient_text: str) -> str:
        """성분 사전에 있으면 표준명으로, 없으면 원문 표기 그대로."""
        from healthcheck.pipeline.step3_claims import _find_ingredients

        found = _find_ingredients(
            ingredient_text, self._patterns.ingredient_lookup, self._patterns.strict_left_ingredients
        )
        return found[0][0] if found else ingredient_text


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalized_with_index(source: str) -> tuple[str, list[int]]:
    """정규화한 문자열과, 정규화 문자 위치 → 원문 위치 표."""
    translated = source.translate(_QUOTE_CHARS)
    chars: list[str] = []
    index: list[int] = []
    for i, char in enumerate(translated):
        if char.isspace():
            continue
        chars.append(char)
        index.append(i)
    return "".join(chars), index


def _intensity(quote: str, patterns) -> str:
    from healthcheck.pipeline.step3_claims import _find_intensity

    intensity, _, _ = _find_intensity(quote, patterns.effect_by_intensity)
    return intensity
