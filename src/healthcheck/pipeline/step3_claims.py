"""③ 주장 구조화 — (성분·제품명, 주장 효능, 표현 강도) 추출.

발표 자료에는 "③ 구조화와 ⑤ 판정은 규칙 기반 알고리즘이 처리한다"고 적혀 있다.
⑤는 확실히 규칙만으로 가능하다. 그러나 ③이 순수 규칙만으로 가능한지는 검증되지
않았다. 자유 발화 자막에서 (성분, 효능, 강도)를 뽑는 일이기 때문이다.

그래서 인터페이스를 먼저 두고 규칙 기반 구현을 먼저 만든다. 한계는 숨기지 않고
tests/unit/test_claim_extractor.py에 xfail로 기록한다. 어떤 문장이 새는지
테스트 목록만 봐도 드러나야 한다.

규칙 기반 추출이 잡는 것과 놓치는 것은 이 모듈 아래쪽 '알려진 한계'에 적었다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import yaml

from healthcheck.domain.types import Claim, ClaimSet, Intensity

logger = logging.getLogger(__name__)

RULES_DIR = Path(__file__).resolve().parents[3] / "rules"
DEFAULT_PATTERNS_PATH = RULES_DIR / "claim_patterns.yaml"
DEFAULT_ALIASES_PATH = RULES_DIR / "ingredient_aliases.yaml"

# 문장 경계. 자막에는 마침표가 아예 없는 경우가 많아서 줄바꿈도 경계로 본다.
_SENTENCE_SPLIT = re.compile(r"[.!?。\n]+")


class ClaimExtractor(Protocol):
    """자막 텍스트에서 주장을 뽑는다."""

    def extract(self, text: str) -> ClaimSet: ...


@dataclass(frozen=True, slots=True)
class _Patterns:
    """규칙 파일에서 읽어 온 표현 목록."""

    disease_cure: tuple[str, ...]
    # 이 표현이 있는 문장은 주장으로 읽지 않는다. 오정보를 바로잡는 문장이다.
    negation: tuple[str, ...]
    effect_by_intensity: tuple[tuple[Intensity, tuple[str, ...]], ...]
    # 자막 어디에 한 번만 나와도 거절하는 표현
    out_of_scope_immediate: tuple[str, ...]
    # 성분 주장이 담긴 문장 안에 함께 있을 때만 거절하는 표현
    out_of_scope_contextual: tuple[str, ...]
    effect_targets: tuple[str, ...]
    # 표기 → 표준 성분명
    ingredient_lookup: dict[str, str]


@lru_cache(maxsize=4)
def load_patterns(
    patterns_path: Path | str = DEFAULT_PATTERNS_PATH,
    aliases_path: Path | str = DEFAULT_ALIASES_PATH,
) -> _Patterns:
    """규칙 파일을 읽는다. 판정 규칙표와 같은 이유로 코드에 넣지 않는다."""
    raw = yaml.safe_load(Path(patterns_path).read_text(encoding="utf-8")) or {}
    aliases = yaml.safe_load(Path(aliases_path).read_text(encoding="utf-8")) or {}

    effects = raw.get("효능_표현") or {}
    disease_cure = tuple(raw.get("질병_치료_표현") or ())

    # 강도가 센 것부터 본다. 한 문장에 '좋다'와 '낫는다'가 같이 있으면 단정으로 읽는다.
    # 질병 치료 표현은 그 자체로 가장 센 단정이므로 ASSERTIVE 목록에 합친다.
    # '완치시킨다'를 완화 표현으로 읽으면 표현 강도가 거꾸로 기록된다.
    by_intensity: list[tuple[Intensity, tuple[str, ...]]] = []
    for intensity in ("ASSERTIVE", "SUGGESTIVE", "UNCERTAIN"):
        expressions = tuple(effects.get(intensity) or ())
        if intensity == "ASSERTIVE":
            expressions = tuple(dict.fromkeys(expressions + disease_cure))
        by_intensity.append((intensity, expressions))

    scope = raw.get("범위_밖_표현") or {}
    immediate = tuple(scope.get("즉시_거절") or ())
    contextual = tuple(scope.get("문맥_판단") or ())

    targets: list[str] = []
    for group in (raw.get("효능_대상") or {}).values():
        targets.extend(group or ())

    lookup: dict[str, str] = {}
    for standard, variants in (aliases.get("성분") or {}).items():
        lookup[standard] = standard
        for variant in variants or ():
            lookup[variant] = standard

    return _Patterns(
        disease_cure=disease_cure,
        negation=tuple(raw.get("부정_표현") or ()),
        effect_by_intensity=tuple(by_intensity),
        out_of_scope_immediate=immediate,
        out_of_scope_contextual=contextual,
        # 긴 표기를 먼저 맞춰야 '혈당'이 '혈당 조절'을 가로채지 않는다.
        effect_targets=tuple(sorted(set(targets), key=len, reverse=True)),
        ingredient_lookup=lookup,
    )


class RuleBasedClaimExtractor:
    """성분명 사전 매칭 + 효능 표현 패턴 매칭.

    외부 호출이 없다. 사전과 패턴 파일만 읽는다.
    """

    def __init__(
        self,
        patterns_path: Path | str = DEFAULT_PATTERNS_PATH,
        aliases_path: Path | str = DEFAULT_ALIASES_PATH,
    ) -> None:
        self._patterns = load_patterns(patterns_path, aliases_path)

    def extract(self, text: str) -> ClaimSet:
        patterns = self._patterns
        text = text or ""

        # 의약품을 대체하라는 표현은 어디에 한 번만 나와도 거절한다.
        # 가장 위험한 유형이므로 문맥을 따지지 않는다.
        out_of_scope = _contains_any(text, patterns.out_of_scope_immediate)

        # 긴 글에서는 성분이 언급된 문장 주변만 본다. 4천 자 블로그 글에서
        # 성분과 아무 관계 없는 문장까지 훑으면 엉뚱한 짝이 생기고, 곁가지
        # 문장 하나 때문에 글 전체 판정이 뒤집힌다.
        sentences = _focus(text, patterns.ingredient_lookup)

        claims: list[Claim] = []
        seen: set[tuple[str, str]] = set()
        disease_cure = False

        for sentence, offset in sentences:
            # 오정보를 바로잡는 문장은 주장으로 읽지 않는다. 이 처리가 없으면
            # "암이 낫는다는 얘기가 절대 아니에요" 같은 문장이 WARNING을
            # 유발한다. 판정 방향이 반대로 뒤집히는 가장 위험한 오판이었다.
            if _contains_any(sentence, patterns.negation):
                logger.debug("부정·반박 문장으로 보고 건너뛴다: %s", sentence[:60])
                continue

            sentence_claims = self._claims_in_sentence(sentence, offset)

            # 질병 치료 표현도 문장 단위로 본다. 글 전체에서 찾으면 4천 자
            # 홍삼 가이드 글에 '완치'가 한 번 나온 것만으로 '주의하세요'가
            # 됐다. 성분이나 효능 대상이 같은 문장에 있을 때만 인정한다.
            if _contains_any(sentence, patterns.disease_cure) and self._is_about_effect(sentence):
                disease_cure = True

            # 의료 이야기에 흔히 나오는 단어는 주장이 실린 문장에 섞였을
            # 때만 거절한다.
            if sentence_claims and _contains_any(sentence, patterns.out_of_scope_contextual):
                out_of_scope = True

            for claim in sentence_claims:
                key = (claim.ingredient, claim.claimed_effect)
                if key not in seen:
                    seen.add(key)
                    claims.append(claim)

        return ClaimSet(
            claims=tuple(claims),
            in_scope=not out_of_scope,
            disease_cure_expression=disease_cure,
            extraction_failed=not claims,
        )

    def _is_about_effect(self, sentence: str) -> bool:
        """이 문장이 성분이나 효능 대상을 말하고 있는가.

        질병 치료 표현이 성분·효능 이야기와 같은 문장에 있어야 주장으로 본다.
        "완치라는 말은 조심해야 합니다" 같은 일반론을 주장으로 읽지 않기 위한
        조건이다. 대명사로 성분을 지칭하는 "이 즙 드시면 당뇨가 낫습니다"는
        효능 대상(당뇨)이 잡혀서 그대로 걸린다.
        """
        patterns = self._patterns
        if _find_ingredients(sentence, patterns.ingredient_lookup):
            return True
        return bool(_find_first_each(sentence, patterns.effect_targets))

    def _claims_in_sentence(self, sentence: str, offset: int) -> list[Claim]:
        patterns = self._patterns

        ingredients = _find_ingredients(sentence, patterns.ingredient_lookup)
        if not ingredients:
            return []

        targets = _find_first_each(sentence, patterns.effect_targets)
        intensity, effect_pos = _find_intensity(sentence, patterns.effect_by_intensity)

        # 효능 표현도 효능 대상도 없으면 주장이 아니다. 성분 이름만 나온 문장이다.
        if not targets and effect_pos is None:
            return []

        claims: list[Claim] = []
        for standard, raw_name, position in ingredients:
            target = _nearest(targets, position)
            effect_text = _effect_text(sentence, target, effect_pos)
            if not effect_text:
                continue
            start = position
            end = position + len(raw_name)
            claims.append(
                Claim(
                    ingredient=standard,
                    claimed_effect=effect_text,
                    intensity=intensity,
                    span=(offset + start, offset + end),
                    raw_ingredient=raw_name if raw_name != standard else None,
                )
            )
        return claims


class LlmClaimExtractor:
    """자리만 비워 둔다. 이번 단계에서는 구현하지 않는다.

    규칙 기반 추출이 놓치는 문장 유형(대명사 지칭, 문장을 넘나드는 주장,
    비유 표현, 반박문 오인)을 메우려면 결국 이 자리가 필요하다.
    다만 도입 여부는 규칙 기반의 한계를 실제 자막으로 측정한 뒤에 정한다.

    ClaimExtractor 인터페이스를 그대로 만족시키면 되므로, 파이프라인 다른
    부분은 건드리지 않고 교체할 수 있다.
    """

    def extract(self, text: str) -> ClaimSet:  # pragma: no cover - 미구현
        raise NotImplementedError(
            "LlmClaimExtractor는 아직 구현하지 않았습니다. "
            "규칙 기반 추출의 한계를 실제 자막으로 측정한 뒤 도입 여부를 정합니다."
        )


# ─────────────────────────────────────────────────────────────
# 내부 도구
# ─────────────────────────────────────────────────────────────


def _sentences(text: str) -> list[tuple[str, int]]:
    """문장과 원문에서의 시작 위치를 함께 돌려준다.

    자막에는 마침표가 없는 경우가 많다. 그런 자막은 전체가 한 문장이 되고,
    멀리 떨어진 성분과 효능이 엉뚱하게 짝지어질 수 있다. 알려진 한계다.
    """
    result: list[tuple[str, int]] = []
    position = 0
    for chunk in _SENTENCE_SPLIT.split(text):
        if chunk.strip():
            start = text.find(chunk, position)
            result.append((chunk, start if start >= 0 else position))
        position += len(chunk) + 1
    return result


# 이보다 긴 글은 성분 언급 주변만 본다. 자막과 기사 모두 이 길이를 넘는다.
_FOCUS_MIN_LENGTH = 1500
# 성분이 언급된 문장의 앞뒤로 몇 문장까지 함께 볼지.
_FOCUS_WINDOW = 1


def _focus(text: str, lookup: dict[str, str]) -> list[tuple[str, int]]:
    """볼 문장을 고른다.

    짧은 글은 전부 본다. 긴 글은 성분이 언급된 문장과 그 앞뒤 한 문장만
    본다. 성분 이야기가 없는 구간에서 주장을 만들 수는 없고, 그 구간의
    곁가지 표현이 판정을 바꾸는 것이 실제 문제였다.

    성분을 한 번도 찾지 못하면 전체를 돌려준다. 성분명 사전이 좁아서
    못 찾은 경우인데, 그때 글을 통째로 버리면 질병 표현도 놓친다.
    """
    sentences = _sentences(text)
    if len(text) <= _FOCUS_MIN_LENGTH or len(sentences) <= 3:
        return sentences

    hits = [
        index
        for index, (sentence, _) in enumerate(sentences)
        if _find_ingredients(sentence, lookup)
    ]
    if not hits:
        return sentences

    keep: set[int] = set()
    for index in hits:
        for offset in range(-_FOCUS_WINDOW, _FOCUS_WINDOW + 1):
            neighbor = index + offset
            if 0 <= neighbor < len(sentences):
                keep.add(neighbor)

    return [sentences[index] for index in sorted(keep)]


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _find_ingredients(
    sentence: str, lookup: dict[str, str]
) -> list[tuple[str, str, int]]:
    """(표준 성분명, 원문 표기, 위치) 목록. 긴 표기를 먼저 맞춘다."""
    found: list[tuple[str, str, int]] = []
    taken: list[tuple[int, int]] = []

    for variant in sorted(lookup, key=len, reverse=True):
        start = sentence.find(variant)
        while start >= 0:
            end = start + len(variant)
            # '홍삼'이 '홍삼농축액' 안에서 다시 잡히는 것을 막는다.
            if not any(a <= start and end <= b for a, b in taken):
                taken.append((start, end))
                found.append((lookup[variant], variant, start))
            start = sentence.find(variant, end)

    return sorted(found, key=lambda item: item[2])


# 한 글자나 두 글자 효능 대상은 다른 단어 안에서 잡힌다. 실제 자막에서
# '장'이 '연구소장'과 '판매량'에, '간'이 '기간 동안'에 걸렸다. 그래서 짧은
# 대상은 앞이 한글이 아니고 뒤가 조사나 관련 명사일 때만 인정한다.
_SHORT_TARGET_MAX = 2
_PARTICLES = "이가은는을를에의도과와만"
_TAIL_WORDS = ("건강", "기능", "질환", "질병")


def _short_target_pattern(needle: str) -> re.Pattern[str]:
    tails = "|".join(_TAIL_WORDS)
    return re.compile(
        rf"(?<![가-힣]){re.escape(needle)}(?=[{_PARTICLES}]|\s*(?:{tails})|\s|$)"
    )


def _find_first_each(sentence: str, needles: tuple[str, ...]) -> list[tuple[str, int]]:
    """효능 대상과 그 위치."""
    found: list[tuple[str, int]] = []
    for needle in needles:
        if len(needle) <= _SHORT_TARGET_MAX:
            match = _short_target_pattern(needle).search(sentence)
            position = match.start() if match else -1
        else:
            position = sentence.find(needle)

        if position >= 0 and not any(
            needle in other and needle != other for other, _ in found
        ):
            found.append((needle, position))
    return sorted(found, key=lambda item: item[1])


def _find_intensity(
    sentence: str, by_intensity: tuple[tuple[Intensity, tuple[str, ...]], ...]
) -> tuple[Intensity, int | None]:
    """표현 강도와 그 표현이 나온 위치.

    강도가 센 것부터 본다. 효능 표현이 하나도 없으면 추측으로 둔다.
    """
    for intensity, expressions in by_intensity:
        positions = [sentence.find(expression) for expression in expressions]
        positions = [p for p in positions if p >= 0]
        if positions:
            return intensity, min(positions)
    return "UNCERTAIN", None


def _nearest(targets: list[tuple[str, int]], position: int) -> tuple[str, int] | None:
    """성분에서 가장 가까운 효능 대상."""
    if not targets:
        return None
    return min(targets, key=lambda item: abs(item[1] - position))


def _effect_text(
    sentence: str, target: tuple[str, int] | None, effect_pos: int | None
) -> str:
    """주장 효능 문구. 효능 대상부터 효능 표현까지의 원문을 잘라 쓴다."""
    if target is None:
        return ""
    start = target[1]
    end = start + len(target[0])
    if effect_pos is not None:
        start = min(start, effect_pos)
        end = max(end, effect_pos)
        # 효능 표현의 끝까지 포함하도록 어절 경계까지 늘린다.
        boundary = sentence.find(" ", end)
        end = len(sentence) if boundary < 0 else boundary
    return sentence[start:end].strip(" ,·")


# ─────────────────────────────────────────────────────────────
# 알려진 한계 — 발표 자료 문구 수정 판단의 근거
# ─────────────────────────────────────────────────────────────
#
# 잡히는 문장
#   · 한 문장에 성분명과 효능 대상, 효능 표현이 함께 있는 경우
#     "홍삼이 면역력에 도움이 된다", "냉동 블루베리가 당뇨를 완치시킨다"
#   · 사전에 등록된 다른 표기로 부른 경우
#     "6년근 홍삼", "유산균", "커큐민"
#
# 새는 문장
#   1. 성분과 효능이 다른 문장에 흩어진 경우
#      "제가 매일 먹는 게 있어요. 그게 홍삼입니다. 이걸 먹고 혈압이 내려갔어요."
#   2. 대명사로 성분을 지칭한 경우
#      "이걸 드시면 낫습니다" — 성분명이 문장에 없어 사전 매칭이 실패한다
#   3. 비유·간접 표현
#      "혈관이 뻥 뚫린다", "몸이 젊어진다" — 효능 대상 목록에 없다
#   4. (해결됨) 부정·반박 문장을 주장으로 오인
#      rules/claim_patterns.yaml의 부정_표현으로 걸러 낸다. 부정 표현이 있는
#      문장은 주장으로 읽지 않는다. 실제 블로그 글의 "만병통치약은 아니며",
#      "그건 과장입니다", "암이 낫는다는 얘기가 절대 아니에요"가 모두 잡힌다.
#      대가가 있다. 부정 표현이 섞인 문장에서는 진짜 주장도 놓친다.
#      오판보다 판정 보류가 안전하다는 원칙에 따라 이 방향을 택했다.
#
#   5. 문장 부호가 없는 자막
#      전체가 한 문장이 되어 멀리 떨어진 성분과 효능이 짝지어진다
#
# 4번이 가장 위험하다. 판정 방향이 반대로 뒤집히기 때문이다.
# 보수적 기본값 원칙과도 충돌한다. 판정 보류가 아니라 오판이다.
