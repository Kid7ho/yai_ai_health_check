"""친구에게 보내기 — 누른 수와 실제로 보낸 수를 센다.

카카오 챗봇의 공유 버튼(share)은 휴대폰 안에서 공유 화면을 열어 누른 사실이 서버로 오지 않는다.
그래서 카카오 공식 공유 기능(JavaScript SDK)을 쓴다(2026-09-30 사용자 결정).

  1. 결과 카드의 [친구에게 보내기]를 우리 주소(/share/{토큰})로 여는 버튼으로 바꾼다
  2. 그 페이지가 열리면 누른 것으로 기록하고, 바로 카카오톡 공유 화면을 띄운다
  3. 실제로 보내면 카카오가 전송 알림(웹훅)을 보내 준다 → 보낸 것으로 기록한다
  4. 공유된 메시지를 받은 사람이 누르면 /s/{토큰}으로 들어와 열린 것으로 기록하고 채널로 보낸다

토큰은 그 결과의 확인 기록 id(event_id)다. KAKAO_JS_KEY가 없으면 1~3은 꺼지고 원래 share 버튼을 쓴다.
공유할 카드 내용(제목·설명·이미지)은 메모리에 하루만 둔다. 응답 기록에는 글 내용을 담지 않기 때문이다.
TODO(확인필요): 카카오톡 안 브라우저에서 페이지가 열리자마자 공유 화면이 자동으로 뜨는지 휴대폰으로 확인한다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from healthcheck.storage.events import new_event

logger = logging.getLogger(__name__)

OPEN_PATH = "/s"
SHARE_PATH = "/share"
CALLBACK_PATH = "/kakao/share-callback"
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
# 카카오 개발자 문서 다운로드 페이지의 2.8.3(2026-09-03). 무결성 값은 받은 파일의 sha384로 계산했다.
SDK_URL = "https://t1.kakaocdn.net/kakao_js_sdk/2.8.3/kakao.min.js"
SDK_INTEGRITY = "sha384-oroumrnFVE0xtgqyDZJARgERibXg2C28380uaUZz2kHDS5CR7tu20eGiOU6GkTpy"
# serverCallbackArgs 키. CHAT_TYPE·HASH_CHAT_ID·TEMPLATE_ID는 예약어라 쓰지 않는다.
CALLBACK_KEY = "share_token"

router = APIRouter()


class ShareCards:
    """공유할 카드 내용을 토큰별로 잠깐 들고 있는다. 재시작하면 사라지고, 그때는 일반 안내로 공유한다."""

    TTL_SECONDS = 24 * 3600
    MAX_ITEMS = 5000

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, dict]] = {}

    def put(self, token: str, card: dict) -> None:
        if len(self._items) >= self.MAX_ITEMS:
            oldest = min(self._items, key=lambda key: self._items[key][0])
            self._items.pop(oldest, None)
        self._items[token] = (time.monotonic(), card)

    def get(self, token: str) -> dict | None:
        item = self._items.get(token)
        if item is None:
            return None
        if time.monotonic() - item[0] > self.TTL_SECONDS:
            self._items.pop(token, None)
            return None
        return item[1]

    def clear(self) -> None:
        self._items.clear()


SHARE_CARDS = ShareCards()


def open_link(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}{OPEN_PATH}/{token}"


def share_page_link(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}{SHARE_PATH}/{token}"


def with_share_link(
    payload: dict,
    base_url: str | None,
    token: str,
    *,
    web_share: bool = False,
    user_key: str | None = None,
) -> dict:
    """공유 버튼이 달린 결과 카드에 추적 주소를 건다. 카드 문구는 바꾸지 않는다.

    web_share면 [친구에게 보내기]를 공유 페이지를 여는 버튼으로 바꾸고, 공유할 내용을 SHARE_CARDS에 둔다.
    아니면 원래 share 버튼을 두고, 카드 사진에만 열림 추적 주소를 건다.
    """
    if not base_url:
        return payload
    for output in payload.get("template", {}).get("outputs", []):
        card = output.get("basicCard")
        if not card or "thumbnail" not in card:
            continue
        buttons = card.get("buttons", [])
        share_index = next((i for i, b in enumerate(buttons) if b.get("action") == "share"), None)
        if share_index is None:
            continue
        card["thumbnail"]["link"] = {"web": open_link(base_url, token)}
        if web_share:
            label = buttons[share_index]["label"]
            buttons[share_index] = {"action": "webLink", "label": label, "webLinkUrl": share_page_link(base_url, token)}
            SHARE_CARDS.put(
                token,
                {
                    "title": card.get("title", ""),
                    "description": card.get("description", ""),
                    "imageUrl": card["thumbnail"].get("imageUrl"),
                    "label": card["thumbnail"].get("altText"),
                    "owner": user_key,
                },
            )
    return payload


# 채널 주소를 모를 때 보여 주는 안내. 판정 결과가 아니라서 면책 문구를 붙이지 않는다.
_FALLBACK_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>AI 건강지킴이</title></head>
<body style="font-family:sans-serif;padding:32px;font-size:22px;line-height:1.6">
<p><b>AI 건강지킴이</b></p>
<p>받은 건강정보가 믿을 만한지 확인해 드려요.</p>
<p>카카오톡에서 <b>AI 건강지킴이</b> 채널을 검색해 추가하고, 링크나 문장을 보내 주세요.</p>
</body></html>"""

# 공유 페이지. 열리자마자 공유 화면을 띄우고, 돌아오면 창을 닫는다. 자동으로 안 뜨면 큰 버튼을 누르게 한다.
_SHARE_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>친구에게 보내기</title>
<script src="{sdk}" integrity="{integrity}" crossorigin="anonymous"></script></head>
<body style="font-family:sans-serif;padding:28px;font-size:22px;line-height:1.6;text-align:center">
<p style="margin-top:40px"><b>카카오톡 공유 화면을 여는 중이에요</b></p>
<button id="send" style="font-size:24px;padding:18px 28px;margin-top:24px;border:0;border-radius:14px;background:#FEE500;width:100%">카카오톡으로 보내기</button>
<button id="close" style="font-size:20px;padding:14px 24px;margin-top:16px;border:1px solid #ccc;border-radius:14px;background:#fff;width:100%">닫기</button>
<script>
var MESSAGE = {message};
function share() {{ try {{ if (!Kakao.isInitialized()) Kakao.init({js_key}); Kakao.Share.sendDefault(MESSAGE); }} catch (e) {{}} }}
function closeView() {{ location.href = "kakaotalk://inappbrowser/close"; }}
document.getElementById("send").onclick = share;
document.getElementById("close").onclick = closeView;
var opened = false;
document.addEventListener("visibilitychange", function () {{
  if (document.visibilityState === "hidden") opened = true;
  else if (opened) closeView();
}});
share();
</script></body></html>"""


def _share_message(card: dict | None, base_url: str, token: str) -> dict:
    """카카오 공유 메시지(feed). 받는 사람이 누르면 열림 추적 주소로 들어온다."""
    link = {"mobileWebUrl": open_link(base_url, token), "webUrl": open_link(base_url, token)}
    if card:
        label = card.get("label") or ""
        content = {
            "title": f"[{label}] {card.get('title', '')}".strip() if label else card.get("title", ""),
            "description": card.get("description", ""),
            "imageUrl": card.get("imageUrl"),
            "imageWidth": 800,
            "imageHeight": 400,
            "link": link,
        }
    else:
        content = {
            "title": "AI 건강지킴이",
            "description": "받은 건강정보가 믿을 만한지 카카오톡으로 확인해 보세요.",
            "imageUrl": None,
            "link": link,
        }
    if not content["imageUrl"]:
        content.pop("imageUrl")
        content.pop("imageWidth", None)
        content.pop("imageHeight", None)
    return {
        "objectType": "feed",
        "content": content,
        "buttons": [{"title": "나도 확인해 보기", "link": link}],
        "serverCallbackArgs": {CALLBACK_KEY: token},
    }


def _script_json(value) -> str:
    # 스크립트 안에 넣으므로 </script>로 끊기지 않게 한다.
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


@router.get(SHARE_PATH + "/{token}", include_in_schema=False)
async def share_page(token: str, request: Request) -> Response:
    """[친구에게 보내기]를 누르면 열린다. 누른 것으로 기록하고 공유 화면을 띄운다."""
    js_key = getattr(request.app.state, "kakao_js_key", None)
    base_url = getattr(request.app.state, "image_base_url", None)
    if not _TOKEN.match(token) or not js_key or not base_url:
        return HTMLResponse(_FALLBACK_HTML, status_code=404)
    card = SHARE_CARDS.get(token)
    await _record(request, "share", ref=token, user_key=(card or {}).get("owner"),
                  outcome=_label_code(card))
    page = _SHARE_HTML.format(
        sdk=SDK_URL,
        integrity=SDK_INTEGRITY,
        message=_script_json(_share_message(card, base_url, token)),
        js_key=_script_json(js_key),
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@router.post(CALLBACK_PATH, include_in_schema=False)
@router.get(CALLBACK_PATH, include_in_schema=False)
async def share_callback(request: Request) -> Response:
    """카카오톡 공유 전송 알림. 실제로 보냈을 때 카카오가 부른다. 3초 안에 2XX를 돌려줘야 한다."""
    admin_key = getattr(request.app.state, "kakao_admin_key", None)
    if admin_key:
        expected = f"KakaoAK {admin_key}"
        if not hmac.compare_digest(request.headers.get("authorization", ""), expected):
            logger.warning("공유 전송 알림의 인증 값이 맞지 않는다")
            return JSONResponse({"ok": False}, status_code=401)
    params: dict = dict(request.query_params)
    if request.method == "POST":
        # 폼(application/x-www-form-urlencoded)과 JSON 둘 다 받는다. 문서에 본문 형식이 정해져 있지 않다.
        raw = (await request.body()).decode("utf-8", errors="replace")
        if raw.strip().startswith("{"):
            try:
                body = json.loads(raw)
                if isinstance(body, dict):
                    params.update(body)
            except ValueError:
                pass
        elif raw:
            params.update({key: values[-1] for key, values in parse_qs(raw).items()})
    token = params.get(CALLBACK_KEY)
    if isinstance(token, dict):
        token = token.get(CALLBACK_KEY)
    if isinstance(token, str) and token.startswith("{"):
        # 문서상 사용자 정의 값의 형식이 JSON으로 적혀 있다. 문자열로 오면 풀어 본다.
        try:
            token = json.loads(token).get(CALLBACK_KEY, token)
        except Exception:
            pass
    chat = params.get("HASH_CHAT_ID")
    await _record(
        request,
        "share_sent",
        ref=token if isinstance(token, str) and _TOKEN.match(token) else None,
        outcome=str(params.get("CHAT_TYPE") or "") or None,
        # 채팅방 해시는 한 번 더 해시해 서로 다른 방 수만 셀 수 있게 한다.
        site=hashlib.sha256(str(chat).encode("utf-8")).hexdigest()[:16] if chat else None,
    )
    return JSONResponse({"ok": True})


@router.get(OPEN_PATH + "/{token}", include_in_schema=False)
async def share_open(token: str, request: Request) -> Response:
    """공유된 메시지(또는 결과 카드 사진)를 누르면 들어온다. 열린 것으로 기록하고 채널로 보낸다."""
    if not _TOKEN.match(token):
        return HTMLResponse(_FALLBACK_HTML, status_code=404)
    await _record(request, "share_open", ref=token)
    channel_url = getattr(request.app.state, "channel_url", None)
    if channel_url:
        return RedirectResponse(channel_url, status_code=302)
    return HTMLResponse(_FALLBACK_HTML)


_LABEL_CODES = {"믿을 만해요": "TRUSTWORTHY", "조심하세요": "CAUTION", "주의하세요": "WARNING", "알 수 없어요": "UNKNOWN"}


def _label_code(card: dict | None) -> str:
    if not card:
        return "none"
    return _LABEL_CODES.get(str(card.get("label") or ""), "none")


async def _record(request: Request, kind: str, *, ref: str | None, user_key: str | None = None,
                  outcome: str | None = None, site: str | None = None) -> None:
    events = getattr(request.app.state, "events", None)
    if events is None:
        return
    try:
        event = new_event(kind, user_key=user_key, outcome=outcome, ref=ref)
        if site:
            from dataclasses import replace

            event = replace(event, site=site)
        await events.record(event)
    except Exception:
        logger.exception("공유 기록 실패 kind=%s", kind)


__all__ = ["SHARE_CARDS", "ShareCards", "router", "with_share_link"]
