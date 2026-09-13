"""⑥ 응답 생성 — 카카오 카드 포맷 + 면책 문구.

이 서비스는 진단하지 않는다. 결과는 "진단"이 아니라 "근거 제시"다.
그래서 면책 문구는 선택 항목이 아니라 구조다.

카카오 카드를 만들 수 있는 공개 함수는 render()와 render_reason() 둘뿐이고,
두 함수 모두 마지막에 면책 블록을 무조건 덧붙인다. 끌 수 있는 인자를 두지 않는다.
호출자가 outputs를 직접 조립할 수 없게 내부 함수는 밑줄로 감춘다.

확인된 카카오 응답 포맷 제약:
  simpleText.text        최대 1000자
  textCard.title         최대 50자
  textCard.description   최대 400자
  buttons                가로 2개 / 세로 3개, 라벨 14자 (가로 2개일 때는 8자)
  action                 webLink | message | phone | block | share | operator

버튼 라벨 "왜 그런지 보기"가 9자라서 가로 배치의 8자 제한을 넘는다.
그래서 buttonLayout을 vertical로 둔다. 세로 배치는 14자까지 허용한다.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import yaml

from healthcheck.domain.labels import LABEL_TEXT, Label
from healthcheck.domain.types import CheckResult

logger = logging.getLogger(__name__)

GUARD_PATH = Path(__file__).resolve().parents[3] / "rules" / "response_guard.yaml"

# 면책 문구. 모든 응답에 들어간다. 이 값을 비우거나 우회할 경로를 만들지 않는다.
DISCLAIMER = "이 결과는 참고용이며 진단이 아닙니다."

# 카카오 포맷 제약
_MAX_SIMPLE_TEXT = 1000
_MAX_TITLE = 50
_MAX_DESCRIPTION = 400
_MAX_BUTTON_LABEL = 14

# 어르신이 읽을 안내 문구
_ACK_TEXT = "받았습니다. 확인하고 있어요.\n잠시만 기다려 주세요 (약 15초)"
# 판정 대신 보내는 안내 문구
_NOTICE_TEXT = {
    "youtube_not_video": (
        "보내 주신 링크는 유튜브 채널이나 재생목록이에요.\n"
        "확인하고 싶은 영상 하나의 링크를 보내 주세요."
    ),
}

def _notice_text(notice: str) -> str:
    """안내 종류에 맞는 문구. 사이트 이름이 붙는 안내는 '종류:이름' 형태로 온다."""
    if notice.startswith("login_required:"):
        site = notice.split(":", 1)[1] or "이 사이트"
        # 막을 수 없는 한계다. 대신 어르신이 할 수 있는 다른 방법을 알려 준다.
        return (
            f"{site} 글은 로그인이 필요해서 내용을 볼 수 없어요.\n"
            "글 내용을 복사해서 보내 주시면 확인해 드릴게요."
        )
    return _NOTICE_TEXT.get(notice, _RETRY_TEXT)


_RETRY_TEXT = (
    "지금은 확인해 드리기 어려워요.\n"
    "잠시 뒤에 다시 보내 주시면 확인해 드릴게요."
)
_OUT_OF_SCOPE_TEXT = (
    "이건 음식이나 건강기능식품 이야기가 아니라서 확인해 드릴 수 없어요.\n"
    "약이나 치료에 관한 것은 꼭 의사 선생님께 물어보세요."
)

_REASON_BUTTON = "왜 그런지 보기"
_SHARE_BUTTON = "가족에게 보내기"

# 금지 표현이 발견됐을 때 대신 내보내는 문구.
# 근거를 못 보여 주더라도 진단처럼 읽히는 문구를 내보내지는 않는다.
_GUARD_FALLBACK = "확인한 근거를 보여 드리기 어려워요."


@lru_cache(maxsize=2)
def _load_guard(path: Path | str = GUARD_PATH) -> tuple[tuple[str, ...], tuple[str, ...]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return tuple(raw.get("금지_표현") or ()), tuple(raw.get("허용_예외") or ())


def find_forbidden(text: str) -> list[str]:
    """어르신에게 나갈 문구에 진단·처방으로 읽히는 표현이 있는지 찾는다.

    이 서비스는 진단하지 않는다. 면책 문구가 붙는지는 따로 강제하지만,
    카드 본문 자체가 진단처럼 읽히면 면책 문구만으로는 부족하다.
    """
    forbidden, allowed = _load_guard()

    remaining = text
    for exception in allowed:
        remaining = remaining.replace(exception, " ")

    return [phrase for phrase in forbidden if phrase in remaining]


def ack_response() -> dict:
    """즉시 ack 응답.

    카카오 스킬 응답 제한은 5초인데 실제 파이프라인은 10~20초가 걸린다.
    template 없이 useCallback만 담아 돌려주고, 결과는 콜백으로 보낸다.
    """
    return {"version": "2.0", "useCallback": True, "data": {"text": _ACK_TEXT}}


def render(result: CheckResult) -> dict:
    """판정 결과를 카카오 응답으로 만든다.

    카카오 카드를 만드는 유일한 경로다. 면책 문구는 항상 붙는다.
    """
    verdict = result.verdict
    label = verdict.label

    # 일시적 실패는 '알 수 없어요'로 답하지 않는다. 다시 보내면 달라질 수 있는
    # 상태인데 판정이 끝난 것처럼 보여 주면 어르신이 다시 시도하지 않는다.
    if result.retryable_failure:
        return _wrap([_simple_text(_RETRY_TEXT)])

    if result.notice:
        return _wrap([_simple_text(_notice_text(result.notice))])

    if not _is_in_scope(result):
        return _wrap([_simple_text(_OUT_OF_SCOPE_TEXT)])

    card = {
        "textCard": {
            "title": _clip(LABEL_TEXT[label], _MAX_TITLE),
            "description": _clip(_describe(result), _MAX_DESCRIPTION),
            "buttonLayout": "vertical",
            "buttons": _buttons(),
        }
    }
    return _wrap([card])


def render_reason(result: CheckResult) -> dict:
    """"왜 그런지 보기"를 눌렀을 때 보여 줄 근거.

    별도 웹페이지를 만들지 않는다. 이번 단계는 말풍선으로만 답한다.
    설명이 400자를 넘을 수 있으므로 카드가 아니라 simpleText를 쓴다.
    """
    verdict = result.verdict
    lines = [f"{LABEL_TEXT[verdict.label]}", ""]
    lines.extend(reason for reason in verdict.reasons if reason)

    sources = _sources(result)
    if sources:
        lines.extend(["", "근거"])
        lines.extend(f"· {source}" for source in sources)

    mentions = _mentions(result)
    if mentions:
        lines.extend(["", *mentions])

    return _wrap([_simple_text("\n".join(lines))])


# ─────────────────────────────────────────────────────────────
# 내부 — 호출자가 직접 쓰지 못하게 감춘다
# ─────────────────────────────────────────────────────────────


def _wrap(outputs: list[dict]) -> dict:
    """응답을 감싸는 유일한 지점.

    여기서 두 가지를 강제한다. 면책 문구를 무조건 덧붙이고, 진단처럼 읽히는
    표현이 섞였는지 검사한다. 이 함수를 거치지 않는 응답 경로를 만들지 않으며,
    둘 중 어느 것도 끌 수 있는 인자를 두지 않는다.
    """
    return {
        "version": "2.0",
        "template": {
            "outputs": [*(_guard(output) for output in outputs), _simple_text(DISCLAIMER)]
        },
    }


def _guard(output: dict) -> dict:
    """금지 표현이 섞인 문구를 안전한 문구로 바꾼다.

    조용히 고치지 않는다. 오류 로그를 남겨 규칙표를 고칠 수 있게 한다.
    금지 표현이 나올 수 있는 경로는 규칙표의 근거 문장이다.
    """
    if "simpleText" in output:
        violations = find_forbidden(output["simpleText"]["text"])
        if violations:
            logger.error("응답 문구에 금지 표현이 있다: %s", violations)
            return _simple_text(_GUARD_FALLBACK)
        return output

    if "textCard" in output:
        card = output["textCard"]
        violations = find_forbidden(f"{card.get('title', '')} {card.get('description', '')}")
        if violations:
            logger.error("카드 문구에 금지 표현이 있다: %s", violations)
            safe = dict(card)
            safe["description"] = _GUARD_FALLBACK
            return {"textCard": safe}
        return output

    return output


def _simple_text(text: str) -> dict:
    return {"simpleText": {"text": _clip(text, _MAX_SIMPLE_TEXT)}}


def _buttons() -> list[dict]:
    """세로 배치 버튼 두 개. 라벨 14자 제한을 지킨다."""
    return [
        {
            "action": "message",
            "label": _clip(_REASON_BUTTON, _MAX_BUTTON_LABEL),
            "messageText": _REASON_BUTTON,
        },
        {"action": "share", "label": _clip(_SHARE_BUTTON, _MAX_BUTTON_LABEL)},
    ]


def _is_in_scope(result: CheckResult) -> bool:
    return result.claims is None or result.claims.in_scope


def _describe(result: CheckResult) -> str:
    """카드 본문. '확인한 내용'과 한 줄 근거를 담는다."""
    lines: list[str] = []

    checked = _checked_claim_text(result)
    if checked:
        lines.extend(["확인한 내용", checked])

    headline = _headline(result)
    if headline:
        lines.extend(["", headline])

    if not lines:
        lines.append("확인할 내용을 찾지 못했어요.")

    return "\n".join(lines)


def _checked_claim_text(result: CheckResult) -> str:
    """무엇을 확인했는지 어르신 말로 돌려준다."""
    claim = result.primary_claim
    if claim is None:
        if result.claims is None or not result.claims.claims:
            return ""
        claim = result.claims.claims[0]
    return f"{claim.ingredient}이(가) {claim.claimed_effect}"


def _headline(result: CheckResult) -> str:
    """판정 근거 한 줄. 규칙표에 적힌 설명을 그대로 쓴다."""
    reasons = [reason for reason in result.verdict.reasons if reason]
    if not reasons:
        return ""
    # 규칙표의 설명은 검수용 문장이라 길다. 카드에는 근거 문장을 쓴다.
    return reasons[-1]


def _mentions(result: CheckResult) -> list[str]:
    """본문에 언급된 브랜드와 전문가 역할. 근거 화면에만 보여 준다.

    판정에는 쓰지 않는다. 어르신이 이 글이 특정 제품을 파는 것인지, 전문가의
    권위를 빌리고 있는지 알아볼 수 있게 하려는 것이다. 실제로 그 역할의 인물인지는
    확인하지 않으므로 그 사실을 함께 적는다.
    """
    claims = result.claims
    if claims is None:
        return []

    lines: list[str] = []
    if claims.brands:
        lines.append("언급된 브랜드: " + ", ".join(claims.brands))
    if claims.authority_roles:
        lines.append("언급된 전문가 역할: " + ", ".join(claims.authority_roles))
        lines.append("실제로 그 역할의 사람인지는 확인하지 않았어요.")
    return lines


def _sources(result: CheckResult) -> list[str]:
    """근거 출처 목록. 식약처 기록과 논문을 함께 보여 준다."""
    sources: list[str] = []

    mfds = result.mfds
    if mfds is not None and not mfds.lookup_failed:
        if mfds.recognition in ("INDIVIDUAL", "NOTIFIED"):
            label = "개별인정형" if mfds.recognition == "INDIVIDUAL" else "고시형"
            sources.append(f"식품의약품안전처 건강기능식품 기능성 원료인정 현황 ({label})")
            if mfds.functionality_text:
                sources.append(f"인정된 기능성: {mfds.functionality_text}")
        if mfds.recognition == "NOT_RECOGNIZED" and mfds.other_recognitions:
            # 성분 자체는 인정 원료인데 주장한 효능은 인정되지 않은 경우다.
            # 이 구분을 알려 주지 않으면 어르신이 "인정 안 된 성분"으로 오해한다.
            sources.append(f"이 성분은 다른 기능성으로는 인정받았습니다: {mfds.other_recognitions[0]}")
            sources.append("다만 이 영상에서 말하는 효능은 인정된 내용이 아닙니다.")
        if mfds.ad_violation_hit:
            sources.append("식품의약품안전처 부당광고 적발 정보")

    academic = result.academic
    if academic is not None and not academic.lookup_failed:
        for paper in academic.papers[:3]:
            sources.append(paper.title)

    return sources


def _clip(text: str, limit: int) -> str:
    """카카오 제한을 넘으면 자른다. 잘린 표시를 남긴다."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


__all__ = [
    "ack_response",
    "render",
    "render_reason",
    "find_forbidden",
    "DISCLAIMER",
    "Label",
]
