"""주소가 아닌 짧은 말 가르기 — 확인 과정에 넣기 전에 바로 답할 말을 고른다.

"안녕하세요"를 보내면 이전에는 확인 과정으로 들어가 "받았습니다(약 15초)"를 보여 주고 15초 뒤에야
"확인하는 대상이 아니에요"라고 답했다. 기다리게 해 놓고 거절하는 셈이다.

규칙 기반 추출은 외부 호출 없이 바로 끝나므로 먼저 돌려 본다. 붙여 넣은 글은 원래 규칙 기반으로만
확인하므로 판단은 확인 과정과 같고 기다림만 없어진다. 순서는 이렇다.

  1. 주장이 잡히거나, 약·치료 이야기(범위 밖), 질병 치료·금지 광고 표현이 있으면 → 확인 과정(None)
  2. 음식 이름·효능 대상이 없는 짧은 인사 → greeting, 짧은 감사·맞장구 → thanks
  3. 60자 이내 질문이고 음식 이름이나 효능 대상이 있으면 → question (이 서비스는 질문에 답하거나 권하지 않는다)
  4. 음식 이름이 없으면 → off_topic
  5. 음식 이름은 있는데 주장이 없으면 → 확인 과정(None). 긴 글은 판단을 서두르지 않는다

말 목록은 rules/guide.yaml에 있다.
"""

from __future__ import annotations

from typing import Literal

from healthcheck.pipeline.step3_claims import RuleBasedClaimExtractor, load_patterns
from healthcheck.pipeline.step6_response import load_guide

SmallTalk = Literal["greeting", "thanks", "question", "off_topic"]

# 이보다 긴 말은 인사·감사로 보지 않는다(띄어쓰기·문장부호 뺀 글자 수).
SHORT_CHARS = 15
# 질문 답은 이보다 짧은 말에만 한다. 붙여 넣은 광고 글에도 "아직도 고민하세요?" 같은 물음이 흔하다.
QUESTION_CHARS = 60


def classify_small_talk(text: str, extractor: RuleBasedClaimExtractor | None = None) -> SmallTalk | None:
    """바로 답할 말이면 그 종류, 확인 과정으로 보낼 말이면 None."""
    text = (text or "").strip()
    if not text:
        return None
    claim_set = (extractor or RuleBasedClaimExtractor()).extract(text)
    if claim_set.claims or not claim_set.in_scope or claim_set.disease_cure_expression or claim_set.prohibited_ad_expression:
        return None

    guide = load_guide()
    compact = "".join(ch for ch in text if ch.isalnum()).lower()
    lowered = text.lower()
    targets = load_patterns().effect_targets
    health_words = claim_set.food_mentioned or any(target in text for target in targets)

    if not health_words and len(compact) <= SHORT_CHARS:
        if _has_any(lowered, guide.get("인사_말")):
            return "greeting"
        if _has_any(lowered, guide.get("감사_말")) or any(compact.startswith(str(w)) for w in guide.get("맞장구_말") or ()):
            return "thanks"
    if health_words and len(compact) <= QUESTION_CHARS and ("?" in text or "？" in text or _has_any(lowered, guide.get("질문_말"))):
        return "question"
    if not claim_set.food_mentioned:
        return "off_topic"
    return None


def _has_any(text: str, words) -> bool:
    return any(str(word).lower() in text for word in words or ())
