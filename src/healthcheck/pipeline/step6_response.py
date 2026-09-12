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

from healthcheck.domain.labels import LABEL_TEXT, Label
from healthcheck.domain.types import CheckResult

# 면책 문구. 모든 응답에 들어간다. 이 값을 비우거나 우회할 경로를 만들지 않는다.
DISCLAIMER = "이 결과는 참고용이며 진단이 아닙니다."

# 카카오 포맷 제약
_MAX_SIMPLE_TEXT = 1000
_MAX_TITLE = 50
_MAX_DESCRIPTION = 400
_MAX_BUTTON_LABEL = 14

# 어르신이 읽을 안내 문구
_ACK_TEXT = "받았습니다. 확인하고 있어요.\n잠시만 기다려 주세요 (약 15초)"
_OUT_OF_SCOPE_TEXT = (
    "이건 음식이나 건강기능식품 이야기가 아니라서 확인해 드릴 수 없어요.\n"
    "약이나 치료에 관한 것은 꼭 의사 선생님께 물어보세요."
)

_REASON_BUTTON = "왜 그런지 보기"
_SHARE_BUTTON = "가족에게 보내기"


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

    return _wrap([_simple_text("\n".join(lines))])


# ─────────────────────────────────────────────────────────────
# 내부 — 호출자가 직접 쓰지 못하게 감춘다
# ─────────────────────────────────────────────────────────────


def _wrap(outputs: list[dict]) -> dict:
    """응답을 감싸는 유일한 지점. 여기서 면책 문구를 무조건 덧붙인다.

    이 함수를 거치지 않는 응답 경로를 만들지 않는다.
    면책 문구를 붙일지 여부를 받는 인자도 두지 않는다.
    """
    return {
        "version": "2.0",
        "template": {"outputs": [*outputs, _simple_text(DISCLAIMER)]},
    }


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


__all__ = ["ack_response", "render", "render_reason", "DISCLAIMER", "Label"]
