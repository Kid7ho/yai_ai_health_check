"""⑥ 응답 생성 — 카카오 카드 포맷 + 면책 문구.

이 서비스는 진단하지 않는다. 결과는 "진단"이 아니라 "근거 제시"다.
그래서 면책 문구는 선택 항목이 아니라 구조다.

판정 결과를 카카오 카드로 만드는 공개 함수는 render()와 render_reason() 둘뿐이고,
두 함수 모두 마지막에 면책 블록을 무조건 덧붙인다. 끌 수 있는 인자를 두지 않는다.
사용법·처음 인사·사진 안내(render_guide 등)는 판정이 아니라서 면책 문구를 붙이지 않는다.
호출자가 outputs를 직접 조립할 수 없게 내부 함수는 밑줄로 감춘다.

확인된 카카오 응답 포맷 제약:
  simpleText.text        최대 1000자
  textCard.title         최대 50자
  textCard.description   최대 400자
  basicCard.title        최대 50자
  basicCard.description  최대 230자, thumbnail 필수(2:1 또는 1:1)
  buttons                가로 2개 / 세로 3개, 라벨 14자 (가로 2개일 때는 8자)
  action                 webLink | message | phone | block | share | operator

버튼 라벨 "왜 그런지 보기"가 9자라서 가로 배치의 8자 제한을 넘는다.
그래서 buttonLayout을 vertical로 둔다. 세로 배치는 14자까지 허용한다.

카카오 응답은 글자색·배경색·버튼색을 지정할 수 없다. 발표 자료 목업의 신호등 색
머리 부분은 라벨별 이미지(basicCard thumbnail)로 대신한다. 이미지는 공개 주소에서
내려받아야 하므로 image_base_url이 없으면 이미지 없는 textCard로 답한다.
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
GUIDE_PATH = Path(__file__).resolve().parents[3] / "rules" / "guide.yaml"

# 면책 문구. 모든 응답에 들어간다. 이 값을 비우거나 우회할 경로를 만들지 않는다.
DISCLAIMER = "이 결과는 참고용이며 진단이 아닙니다."

# 카카오 포맷 제약
_MAX_SIMPLE_TEXT = 1000
_MAX_TITLE = 50
_MAX_DESCRIPTION = 400
_MAX_BASIC_DESCRIPTION = 230
_MAX_BUTTON_LABEL = 14

# 어르신이 읽을 안내 문구
_ACK_TEXT = "받았습니다. 확인하고 있어요.\n잠시만 기다려 주세요 (약 15초)"
# 판정 대신 보내는 안내 문구
_NOTICE_TEXT = {
    "not_target": (
        "본 서비스에서 확인하는 대상이 아니에요.\n"
        "음식·식품·건강기능식품이 건강에 좋다고 말하는 글이나 영상을 보내 주세요."
    ),
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
_SHARE_BUTTON = "친구에게 보내기"
_CHECKED_TITLE = "확인한 내용"
_REASON_TITLE = "왜 그런가요"

# 라벨별 머리 이미지. 그림을 바꾸면 버전을 올린다. 카카오가 같은 주소의 이미지를
# 캐시해 두면 바꾼 그림이 보이지 않는다.
IMAGE_VERSION = "v1"
IMAGE_FILES: dict[Label, str] = {
    Label.TRUSTWORTHY: "trustworthy",
    Label.CAUTION: "caution",
    Label.WARNING: "warning",
    Label.UNKNOWN: "unknown",
}


def verdict_image_url(image_base_url: str, label: Label) -> str:
    """라벨 머리 이미지의 공개 주소."""
    return f"{image_base_url.rstrip('/')}/static/verdict/{IMAGE_VERSION}/{IMAGE_FILES[label]}.png"

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
    2026-09-19 휴대폰 확인: 기다리는 동안 보이는 말은 이 data.text가 아니라 오픈빌더의 대기 문구 설정이 보인다.
    """
    return {"version": "2.0", "useCallback": True, "data": {"text": _ACK_TEXT}}


def render(result: CheckResult, *, image_base_url: str | None = None, intro: str | None = None) -> dict:
    """판정 결과를 카카오 응답으로 만든다.

    카카오 카드를 만드는 유일한 경로다. 면책 문구는 항상 붙는다.
    카드에는 라벨과 확인한 내용만 둔다. 판정 이유(규칙표 안내)는 "왜 그런지 보기"를
    눌렀을 때 보여 준다. 발표 자료 목업의 결과 화면과 같은 구성이다.
    """
    verdict = result.verdict
    label = verdict.label

    # 일시적 실패는 '알 수 없어요'로 답하지 않는다. 다시 보내면 달라질 수 있는
    # 상태인데 판정이 끝난 것처럼 보여 주면 어르신이 다시 시도하지 않는다.
    if result.retryable_failure:
        return _wrap(_intro(intro) + [_simple_text(_RETRY_TEXT)])

    if result.notice:
        return _wrap(_intro(intro) + [_simple_text(_notice_text(result.notice))], quick_replies=_guide_quick_replies())

    if not _is_in_scope(result):
        return _wrap(_intro(intro) + [_simple_text(_OUT_OF_SCOPE_TEXT)], quick_replies=_guide_quick_replies())

    checked = _checked_claim_text(result)
    checked_title = _checked_title(result)

    if image_base_url:
        card = {
            "basicCard": {
                "thumbnail": {
                    "imageUrl": verdict_image_url(image_base_url, label),
                    "altText": LABEL_TEXT[label],
                },
                # 카카오 카드는 제목을 진한 글씨, 설명을 회색 글씨로 보여 준다. 어르신이 가장
                # 먼저 읽어야 할 판정 대상 문장을 제목에 둔다. 제목은 50자 제한이다.
                "title": _clip(checked, _MAX_TITLE) if checked else _NO_CLAIM_TEXT,
                "description": _checked_caption(result) if checked else "",
                "buttons": _buttons(),
            }
        }
        return _wrap(_intro(intro) + [card], quick_replies=_unknown_quick_replies(label))

    card = {
        "textCard": {
            "title": _clip(LABEL_TEXT[label], _MAX_TITLE),
            "description": _clip(_describe(result), _MAX_DESCRIPTION),
            "buttonLayout": "vertical",
            "buttons": _buttons(),
        }
    }
    return _wrap(_intro(intro) + [card], quick_replies=_unknown_quick_replies(label))


def _intro(intro: str | None) -> list[dict]:
    """결과 앞에 붙는 안내 말풍선. 판정 문구가 아니므로 금지 표현 검사만 받는다(_wrap에서)."""
    return [_simple_text(intro)] if intro else []


def try_intro() -> str:
    """[한번 해 보기] 결과 위에 붙는 말. 어떤 문장을 확인했는지와 보내는 법을 알려 준다."""
    return str(load_guide().get("체험_안내", "")).replace("{문장}", try_sentence())


def render_reason(result: CheckResult, *, image_base_url: str | None = None) -> dict:
    """"왜 그런지 보기"를 눌렀을 때 보여 줄 근거.

    별도 웹페이지를 만들지 않는다. 판정 이유는 카드로, 출처와 언급된 브랜드·역할은
    말풍선으로 나눈다. 카드 설명은 230자 제한이라 출처 목록이 들어가지 않는다.
    이미지 주소가 없으면 한 말풍선에 모두 담는다.
    """
    verdict = result.verdict
    # 어르신용 안내를 보여 준다. 규칙표의 근거 문장은 검수용이라 보여 주지 않는다.
    reason = verdict.guidance or "\n".join(r for r in verdict.reasons if r)

    details: list[str] = []
    sources = _sources(result)
    if sources:
        details.append("근거")
        details.extend(f"· {source}" for source in sources)
    mentions = _mentions(result)
    if mentions:
        if details:
            details.append("")
        details.extend(mentions)

    if image_base_url:
        card = {
            "basicCard": {
                "thumbnail": {
                    "imageUrl": verdict_image_url(image_base_url, verdict.label),
                    "altText": LABEL_TEXT[verdict.label],
                },
                "title": _REASON_TITLE,
                # 출처를 따로 말풍선으로 두지 않고 카드 안에 한두 줄로 줄여 넣는다.
                "description": "\n\n".join(
                    part for part in (reason or _NO_CLAIM_TEXT, _compact_sources(result)) if part
                ),
            }
        }
        link = _paper_link(result)
        if link is not None:
            card["basicCard"]["buttons"] = [link]
        return _wrap([card])

    lines = [f"{LABEL_TEXT[verdict.label]}", ""]
    if reason:
        lines.append(reason)
    if details:
        lines.extend(["", *details])
    return _wrap([_simple_text("\n".join(lines))])


# ─────────────────────────────────────────────────────────────
# 내부 — 호출자가 직접 쓰지 못하게 감춘다
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# 사용법 안내 — 판정 결과가 아니다
# ─────────────────────────────────────────────────────────────


@lru_cache(maxsize=2)
def load_guide(path: Path | str = GUIDE_PATH) -> dict:
    """rules/guide.yaml. 문구를 코드 밖에 두어 개발자가 아니어도 고칠 수 있게 한다."""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


# 사용법 카드 사진. 사진을 바꾸면 버전을 올린다. 카카오가 같은 주소의 이미지를 오래 기억한다.
GUIDE_IMAGE_VERSION = "v2"


def guide_image_names() -> tuple[str, ...]:
    """서버가 내보내도 되는 사용법 사진 이름. 경로 조작으로 다른 파일을 읽지 못하게 이 목록만 연다."""
    return tuple(str(card["사진"]) for card in load_guide().get("사용법_카드") or () if card.get("사진"))


def render_guide(image_base_url: str | None = None) -> dict:
    """사용법 카드(넘겨 보는 카드). 마지막 장에 [한번 해 보기] 버튼을 단다.

    공개 주소가 있으면 실제 휴대폰 화면 사진 카드(basicCard)로, 없으면 글자 카드(textCard)로 보낸다.
    """
    guide = load_guide()
    cards = guide.get("사용법_카드") or []
    with_images = bool(image_base_url) and all(card.get("사진") for card in cards)
    limit = _MAX_BASIC_DESCRIPTION if with_images else _MAX_DESCRIPTION
    items = []
    for index, card in enumerate(cards):
        item = {
            "title": _clip(str(card.get("제목", "")), _MAX_TITLE),
            "description": _clip(str(card.get("설명", "")), limit),
        }
        if with_images:
            item["thumbnail"] = {
                "imageUrl": f"{image_base_url.rstrip('/')}/static/guide/{GUIDE_IMAGE_VERSION}/{card['사진']}.png",
                "altText": str(card.get("제목", "")),
                "fixedRatio": True,
            }
        if index == len(cards) - 1:
            item["buttons"] = [_try_button()]
        items.append(item)
    # 2026-09-19 휴대폰에서 textCard 캐러셀 넘겨 보기를 확인했다.
    carousel = {"carousel": {"type": "basicCard" if with_images else "textCard", "items": items}}
    return _wrap_info([carousel], quick_replies=[_try_quick_reply()])


def render_caution() -> dict:
    """주의사항 카드. 진단이 아니라는 점과 약은 의사에게 묻는다는 점을 먼저 둔다."""
    guide = load_guide()
    caution = guide.get("주의사항") or {}
    card = {
        "textCard": {
            "title": _clip(str(caution.get("제목", "")), _MAX_TITLE),
            "description": _clip(str(caution.get("설명", "")), _MAX_DESCRIPTION),
        }
    }
    return _wrap_info([card], quick_replies=[_quick_reply(guide["버튼"]["사용법"]), _try_quick_reply()])


def render_welcome() -> dict:
    """처음 인사. 채널에 처음 들어오거나 "처음"을 보냈을 때."""
    guide = load_guide()
    return _wrap_info(
        [_simple_text(str(guide.get("처음_인사", "")))],
        quick_replies=[_quick_reply(guide["버튼"]["사용법"]), _try_quick_reply()],
    )


def render_small_talk(kind: str) -> dict:
    """주소가 아닌 짧은 말에 바로 답한다(인사·감사·질문·무관). 판정이 아니다."""
    guide = load_guide()
    text = str((guide.get("답") or {}).get(
        {"greeting": "인사", "thanks": "감사", "question": "질문", "off_topic": "무관"}[kind], ""))
    return _wrap_info(
        [_simple_text(text)],
        quick_replies=[_quick_reply(guide["버튼"]["사용법"]), _try_quick_reply()],
    )


def render_media_notice() -> dict:
    """사진·파일을 보냈을 때. 이미지 판독은 범위 밖이다."""
    return _wrap_info([_simple_text(str(load_guide().get("사진_안내", "")))], quick_replies=_guide_quick_replies())


def guide_command(utterance: str) -> str | None:
    """사용법·처음 인사·체험 버튼 중 어느 말인지. 해당하지 않으면 None.

    띄어쓰기·문장부호·물음표를 빼고 비교한다. "사용법?", "어떻게 써요?"도 같은 말로 본다.
    """
    key = _command_key(utterance)
    if not key:
        return None
    guide = load_guide()
    # 체험 버튼은 "한번 해 보기" 한 줄이나, 그 아래 예시 문장까지 두 줄로 온다.
    try_key = _command_key(guide["버튼"]["체험"])
    if key == try_key or key == _command_key(try_message()):
        return "try"
    if key in {_command_key(w) for w in guide.get("사용법_말") or ()} | {_command_key(guide["버튼"]["사용법"])}:
        return "guide"
    if key in {_command_key(w) for w in guide.get("처음_말") or ()}:
        return "welcome"
    if key in {_command_key(w) for w in guide.get("주의사항_말") or ()}:
        return "caution"
    return None


def try_sentence() -> str:
    """[한번 해 보기]를 누르면 대신 확인하는 예시 문장."""
    return str(load_guide().get("체험_문장", ""))


def _command_key(text: str) -> str:
    return "".join(ch for ch in (text or "") if ch.isalnum())


def try_message() -> str:
    """[한번 해 보기] 버튼이 보내는 말. 어르신 말풍선에 버튼 이름 아래 예시 문장이 함께 보인다.

    어르신이 무엇을 보내면 되는지 자기 말풍선으로 보고 배우게 한다. 서버는 첫 줄로 체험 버튼임을 알아본다.
    """
    guide = load_guide()
    return f"{guide['버튼']['체험']}\n{try_sentence()}"


def _try_quick_reply() -> dict:
    guide = load_guide()
    return {"label": _clip(guide["버튼"]["체험"], _MAX_BUTTON_LABEL), "action": "message", "messageText": try_message()}


def _try_button() -> dict:
    guide = load_guide()
    return {"action": "message", "label": _clip(guide["버튼"]["체험"], _MAX_BUTTON_LABEL), "messageText": try_message()}


def _quick_reply(label: str) -> dict:
    return {"label": _clip(label, _MAX_BUTTON_LABEL), "action": "message", "messageText": label}


def _message_button(label: str) -> dict:
    return {"action": "message", "label": _clip(label, _MAX_BUTTON_LABEL), "messageText": label}


def _guide_quick_replies() -> list[dict]:
    return [_quick_reply(load_guide()["버튼"]["사용법"])]


def _unknown_quick_replies(label: Label) -> list[dict] | None:
    """'알 수 없어요'는 어르신이 무엇을 잘못 보냈는지 모를 수 있어 사용법 버튼을 단다."""
    return _guide_quick_replies() if label == Label.UNKNOWN else None


def _wrap_info(outputs: list[dict], *, quick_replies: list[dict] | None = None) -> dict:
    """판정이 아닌 안내 응답. 금지 표현 검사는 똑같이 하고, 면책 문구는 붙이지 않는다.

    안내에 면책 문구를 붙이지 않는 것은 skill.py의 안내 문구(_simple)와 같은 기준이다.
    판정 결과는 반드시 _wrap()을 지나며 거기서 면책 문구가 붙는다.
    """
    template: dict = {"outputs": [_guard(output) for output in outputs]}
    if quick_replies:
        template["quickReplies"] = quick_replies
    return {"version": "2.0", "template": template}


def _wrap(outputs: list[dict], *, quick_replies: list[dict] | None = None) -> dict:
    """응답을 감싸는 유일한 지점.

    여기서 두 가지를 강제한다. 면책 문구를 무조건 덧붙이고, 진단처럼 읽히는
    표현이 섞였는지 검사한다. 이 함수를 거치지 않는 응답 경로를 만들지 않으며,
    둘 중 어느 것도 끌 수 있는 인자를 두지 않는다.
    """
    guarded = [_guard(output) for output in outputs]
    # 면책 문구를 따로 말풍선으로 두지 않고 마지막 말풍선 안에 붙인다. 어르신 화면에
    # 확인 한 번마다 말풍선이 여러 개 쌓여 가독성이 떨어졌다. 붙이는 곳만 바뀌었고
    # 모든 응답에 무조건 붙는다는 점은 그대로다.
    guarded[-1] = _with_disclaimer(guarded[-1])
    template: dict = {"outputs": guarded}
    if quick_replies:
        template["quickReplies"] = quick_replies
    return {"version": "2.0", "template": template}


def _with_disclaimer(output: dict) -> dict:
    """말풍선 끝에 면책 문구를 붙인다. 글자 수 제한에 걸리면 본문을 줄이고 문구는 남긴다."""
    suffix = f"\n\n{DISCLAIMER}"
    for kind, limit in (("basicCard", _MAX_BASIC_DESCRIPTION), ("textCard", _MAX_DESCRIPTION)):
        if kind in output:
            card = dict(output[kind])
            body = _clip(card.get("description", ""), limit - len(suffix))
            card["description"] = f"{body}{suffix}" if body else DISCLAIMER
            return {kind: card}
    if "simpleText" in output:
        body = _clip(output["simpleText"]["text"], _MAX_SIMPLE_TEXT - len(suffix))
        return {"simpleText": {"text": f"{body}{suffix}" if body else DISCLAIMER}}
    return output


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

    for kind in ("textCard", "basicCard"):
        if kind not in output:
            continue
        card = output[kind]
        violations = find_forbidden(f"{card.get('title', '')} {card.get('description', '')}")
        if violations:
            logger.error("카드 문구에 금지 표현이 있다: %s", violations)
            safe = dict(card)
            safe["title"] = _CHECKED_TITLE if kind == "basicCard" else card.get("title", "")
            safe["description"] = _GUARD_FALLBACK
            return {kind: safe}
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


_NO_CLAIM_TEXT = "확인할 내용을 찾지 못했어요."


def _describe(result: CheckResult) -> str:
    """이미지 없는 카드 본문. 확인한 내용과 판정 이유 한 줄. 자세한 근거는 근거 화면에 있다."""
    checked = _checked_claim_text(result)
    if checked:
        reason = result.verdict.guidance.strip()
        return "\n".join(part for part in (_checked_title(result), checked, reason) if part)
    return _NO_CLAIM_TEXT


def _checked_caption(result: CheckResult) -> str:
    """이미지 카드의 회색 설명. 판정 이유 한 줄(규칙표 안내)을 바로 보여 준다.

    이전에는 "이 내용을 확인했어요"만 적어 이유를 보려면 "왜 그런지 보기"를 눌러야 했다(2026-09-19 개선).
    영상·글이면 판정한 것이 그 안의 한 문장이라는 점을 앞에 짧게 남긴다.
    """
    reason = result.verdict.guidance.strip()
    title = _checked_title(result)
    prefix = "" if title == _CHECKED_TITLE else _PART_NOTE.get(title, "")
    return "\n".join(part for part in (prefix, reason) if part) or title


# 영상·글 속 한 문장만 판정했다는 짧은 표시
_PART_NOTE = {
    "글 속 이 내용을 확인했어요": "글 속 이 문장을 확인했어요.",
    "영상 속 이 내용을 확인했어요": "영상 속 이 문장을 확인했어요.",
}


def _checked_title(result: CheckResult) -> str:
    """판정한 것이 영상·글 전체가 아니라 그 안의 한 문장임을 밝힌다.

    "무릎 관절 건강" 영상에서 "칼슘·비타민D가 뼈 건강에 도움"을 판정해 믿을 만해요가
    나갔다. 제목만 보고 영상 전체가 믿을 만하다고 받아들이지 않게 한다.
    """
    source = result.content.source if result.content is not None else None
    if source == "article":
        return "글 속 이 내용을 확인했어요"
    if source in (None, "text"):
        return _CHECKED_TITLE
    return "영상 속 이 내용을 확인했어요"


def _checked_claim_text(result: CheckResult) -> str:
    """무엇을 확인했는지 어르신 말로 돌려준다."""
    claim = result.primary_claim
    if claim is None:
        if result.claims is None or not result.claims.claims:
            return ""
        claim = result.claims.claims[0]
    if claim.statement:
        return claim.statement
    return f"{claim.ingredient}{_subject_particle(claim.ingredient)} {claim.claimed_effect}"


# 받침 여부. 숫자로 끝나는 이름("오메가3")은 읽는 소리로 본다.
_DIGIT_HAS_FINAL = {"0": True, "1": True, "2": False, "3": True, "4": False,
                    "5": False, "6": True, "7": True, "8": True, "9": False}


def _subject_particle(word: str) -> str:
    """이/가를 고른다. 판단할 수 없으면 '이(가)'로 둔다."""
    last = word.strip()[-1:] if word.strip() else ""
    if "가" <= last <= "힣":
        return "이" if (ord(last) - ord("가")) % 28 else "가"
    if last in _DIGIT_HAS_FINAL:
        return "이" if _DIGIT_HAS_FINAL[last] else "가"
    return "이(가)"


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
            # "고시형·개별인정형"은 어르신에게 낯선 행정 용어다.
            label = (
                "개별 심사로 인정된 원료"
                if mfds.recognition == "INDIVIDUAL"
                else "나라가 정한 기능성 원료"
            )
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
        # 영어 논문 제목은 어르신이 읽을 수 없다. 편수로 요약하고 원문은 링크 버튼으로 연다.
        if academic.papers:
            # 찬반을 판단할 수 없는 논문도 편수에 들어간다. "9편 (뒷받침 4편)"은
            # 합이 맞지 않아 보여서 "중"을 붙인다.
            sources.append(
                f"관련 연구 {len(academic.papers)}편 중 "
                f"뒷받침 {academic.supporting}편, 반박 {academic.refuting}편"
            )
        else:
            sources.append("관련 연구를 찾지 못했어요")

    return sources


_PAPER_BUTTON = "연구 자세히 보기"


def _compact_sources(result: CheckResult) -> str:
    """근거 카드에 들어갈 짧은 출처. 카드 설명이 230자라 핵심만 남긴다."""
    lines: list[str] = []
    mfds = result.mfds
    if mfds is not None and mfds.lookup_failed:
        lines.append("식약처: 지금 확인하지 못함")
    if mfds is not None and not mfds.lookup_failed:
        if mfds.recognition in ("INDIVIDUAL", "NOTIFIED"):
            lines.append("식약처: 인정된 기능성 원료")
        elif mfds.recognition == "NOT_RECOGNIZED":
            lines.append("식약처: 이 효능은 인정되지 않음")
    academic = result.academic
    if academic is not None and not academic.lookup_failed:
        if academic.papers:
            lines.append(
                f"연구: {len(academic.papers)}편 중 뒷받침 {academic.supporting}편, 반박 {academic.refuting}편"
            )
        else:
            lines.append("연구: 관련 연구를 찾지 못함")
    claims = result.claims
    if claims is not None and claims.authority_roles:
        # 권위를 빌리는 글인지 알아볼 수 있게 남긴다. 실제 인물인지는 확인하지 않는다.
        lines.append(f"언급된 역할: {', '.join(claims.authority_roles[:2])} (확인 안 함)")
    return "\n".join(lines)


def _paper_link(result: CheckResult) -> dict | None:
    """근거 화면의 연구 원문 링크 버튼. 뒷받침·반박 논문을 먼저 연다."""
    academic = result.academic
    if academic is None or academic.lookup_failed:
        return None
    papers = sorted(academic.papers, key=lambda p: p.stance == "NEUTRAL")
    for paper in papers:
        if paper.url.startswith(("https://", "http://")):
            return {"action": "webLink", "label": _PAPER_BUTTON, "webLinkUrl": paper.url}
    return None


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
    "render_guide",
    "guide_image_names",
    "GUIDE_IMAGE_VERSION",
    "render_welcome",
    "render_media_notice",
    "render_small_talk",
    "render_caution",
    "guide_command",
    "try_sentence",
    "try_message",
    "try_intro",
    "find_forbidden",
    "verdict_image_url",
    "IMAGE_FILES",
    "IMAGE_VERSION",
    "DISCLAIMER",
    "Label",
]
