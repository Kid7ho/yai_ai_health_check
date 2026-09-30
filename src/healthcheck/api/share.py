"""친구에게 보내기 — 누른 수와 실제로 보낸 수를 센다.

카카오 챗봇의 공유 버튼(share)은 휴대폰 안에서 공유 화면을 열어 누른 사실이 서버로 오지 않는다.
그래서 카카오 공식 공유 기능(JavaScript SDK)을 쓴다(2026-09-30 사용자 결정).

  1. 결과 카드의 [친구에게 보내기]를 우리 주소(/share/{토큰})로 여는 버튼으로 바꾼다
  2. 그 페이지가 열리면 누른 것으로 기록하고, 바로 카카오톡 공유 화면을 띄운다
  3. 실제로 보내면 카카오가 전송 알림(웹훅)을 보내 준다 → 보낸 것으로 기록한다
  4. 공유된 메시지를 받은 사람이 누르면 /s/{토큰}(채팅방·채널 홈) 또는 /o/{토큰}(원본)으로 들어와 열린 것으로 기록한다
  5. 공유 페이지는 전송 알림을 받았는지 물어 보고, 보냈으면 스스로 닫는다

토큰은 그 결과의 확인 기록 id(event_id)다. KAKAO_JS_KEY가 없으면 1~3은 꺼지고 원래 share 버튼을 쓴다.
공유할 카드 내용(제목·설명·이미지)은 메모리에 하루만 둔다. 응답 기록에는 글 내용을 담지 않기 때문이다.
TODO(확인필요): 카카오톡 안 브라우저에서 페이지가 열리자마자 공유 화면이 자동으로 뜨는지 휴대폰으로 확인한다.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import re
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from healthcheck.storage.events import new_event

logger = logging.getLogger(__name__)

OPEN_PATH = "/s"
SHARE_PATH = "/share"
CALLBACK_PATH = "/kakao/share-callback"
SOURCE_PATH = "/o"
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
    source_url: str | None = None,
) -> dict:
    """공유 버튼이 달린 결과 카드에 추적 주소를 건다. 카드 문구는 바꾸지 않는다.

    web_share면 [친구에게 보내기]를 공유 페이지를 여는 버튼으로 바꾸고, 공유할 내용을 SHARE_CARDS에 둔다.
    아니면 원래 share 버튼을 두고, 카드 사진에만 열림 추적 주소를 건다.
    source_url은 확인한 원본(영상·기사) 주소다. 공유 메시지의 [원본 보기]에 쓴다.
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
                    "source": source_url if _is_web_url(source_url) else None,
                },
            )
    return payload


def _is_web_url(value: str | None) -> bool:
    return bool(value) and value.startswith(("https://", "http://")) and len(value) <= 2000


# 전송 알림을 받은 시각. 공유 페이지가 이 값을 물어 보고, 보낸 것이 확인되면 스스로 닫는다.
_SENT_AT: dict[str, float] = {}

_FONT = '-apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Noto Sans KR", "Malgun Gothic", sans-serif'
_STYLE = """
:root {{ --bg:#F4F2EE; --card:#FFFFFF; --ink:#191919; --muted:#6B6B6B; --line:#E6E3DD; --kakao:#FEE500; }}
* {{ box-sizing:border-box; }}
html, body {{ margin:0; background:var(--bg); color:var(--ink); font-family:{font}; -webkit-text-size-adjust:100%; }}
main {{ max-width:480px; margin:0 auto; padding:28px 20px 32px; }}
.brand {{ font-size:17px; font-weight:700; color:var(--muted); letter-spacing:-0.2px; }}
h1 {{ font-size:34px; line-height:1.3; margin:10px 0 6px; letter-spacing:-0.8px; }}
.lead {{ font-size:19px; line-height:1.55; color:var(--muted); margin:0 0 22px; }}
.preview {{ background:var(--card); border:1px solid var(--line); border-radius:20px; overflow:hidden; }}
.preview img {{ display:block; width:100%; aspect-ratio:2/1; object-fit:cover; background:#EEE; }}
.preview .body {{ padding:16px 18px 18px; }}
.chip {{ display:inline-block; font-size:16px; font-weight:700; padding:4px 12px; border-radius:999px; background:var(--bg); }}
.claim {{ font-size:21px; line-height:1.45; font-weight:700; margin:0; word-break:keep-all; }}
.chip + .claim {{ margin-top:10px; }}
.primary {{ display:flex; align-items:center; justify-content:center; gap:10px; width:100%; min-height:64px; margin-top:22px;
  border:0; border-radius:16px; background:var(--kakao); color:#191919; font-size:22px; font-weight:800; font-family:inherit; }}
.secondary {{ width:100%; min-height:56px; margin-top:12px; border:1px solid var(--line); border-radius:16px;
  background:transparent; color:var(--ink); font-size:19px; font-weight:600; font-family:inherit; }}
.status {{ text-align:center; font-size:18px; color:var(--muted); margin-top:16px; min-height:26px; }}
.status.done {{ color:#1E7B3C; font-weight:700; }}
"""

# 공유 페이지. 열리자마자 공유 화면을 띄운다. 보낸 것이 확인되면(전송 알림) 스스로 닫는다.
# 자동으로 안 뜨면 노란 버튼을 누르게 한다. 판정 카드 문구는 카드 그대로 보여 준다.
_SHARE_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>친구에게 보내기</title><style>{style}</style>
<script src="{sdk}" integrity="{integrity}" crossorigin="anonymous"></script></head>
<body><main>
<div class="brand">AI 건강지킴이</div>
<h1>친구에게 보내기</h1>
<p class="lead">카카오톡에서 받을 사람을 고르면<br>아래 결과가 그대로 전달돼요.</p>
{preview}
<button id="send" class="primary" type="button"><span aria-hidden="true">💬</span>카카오톡으로 보내기</button>
<button id="close" class="secondary" type="button">닫기</button>
<p id="status" class="status" role="status">보내고 나면 이 창은 저절로 닫혀요.</p>
</main>
<script>
var MESSAGE = {message};
var SENT_URL = {sent_url};
var OPENED_AT = {opened_at};
var statusEl = document.getElementById("status");
function closeView() {{
  location.href = "kakaotalk://inappbrowser/close";
  setTimeout(function () {{ try {{ window.close(); }} catch (e) {{}} }}, 300);
}}
function share() {{
  try {{ if (!Kakao.isInitialized()) Kakao.init({js_key}); Kakao.Share.sendDefault(MESSAGE); }}
  catch (e) {{ statusEl.textContent = "노란 버튼을 눌러 주세요."; }}
}}
var closing = false;
function checkSent() {{
  if (closing) return;
  fetch(SENT_URL + "?after=" + OPENED_AT, {{ cache: "no-store" }})
    .then(function (r) {{ return r.json(); }})
    .then(function (data) {{
      if (data.sent && !closing) {{
        closing = true;
        statusEl.textContent = "보냈어요! 창을 닫을게요.";
        statusEl.className = "status done";
        setTimeout(closeView, 500);
      }}
    }})
    .catch(function () {{}});
}}
document.getElementById("send").onclick = share;
document.getElementById("close").onclick = closeView;
// 공유 화면에서 돌아오면 바로 확인한다. 그 사이에도 0.5초마다 확인하고, 10분이 지나면 멈춘다.
document.addEventListener("visibilitychange", function () {{ if (document.visibilityState === "visible") checkSent(); }});
window.addEventListener("focus", checkSent);
window.addEventListener("pageshow", checkSent);
var poll = setInterval(checkSent, 500);
setTimeout(function () {{ clearInterval(poll); }}, 600000);
share();
</script></body></html>"""

_CHIP_COLORS = {"믿을 만해요": "#E3F4E8", "조심하세요": "#FFF1D6", "주의하세요": "#FDE3E1", "알 수 없어요": "#ECEBF0"}


def _preview_html(card: dict | None) -> str:
    if not card:
        return ""
    label = html.escape(str(card.get("label") or ""))
    image = card.get("imageUrl")
    # 신호등 이미지에 판정이 이미 크게 적혀 있으므로 이미지가 없을 때만 판정 표시를 따로 단다.
    chip = (
        f'<span class="chip" style="background:{_CHIP_COLORS.get(card.get("label"), "#F4F2EE")}">{label}</span>'
        if label and not image
        else ""
    )
    img = f'<img src="{html.escape(image)}" alt="{label}">' if image else ""
    return (
        f'<div class="preview">{img}<div class="body">{chip}'
        f'<p class="claim">{html.escape(str(card.get("title") or ""))}</p></div></div>'
    )


def _share_message(card: dict | None, base_url: str, token: str, *, secret: str | None) -> dict:
    """카카오 공유 메시지(feed). 링크는 모두 우리 주소를 거쳐 열린 수를 센다(웹 도메인에 등록된 주소만 쓸 수 있다)."""
    chat = _web_link(open_link(base_url, token))
    home = _web_link(open_link(base_url, token) + "?to=home")
    if card:
        label = card.get("label") or ""
        content = {
            "title": f"[{label}] {card.get('title', '')}".strip() if label else card.get("title", ""),
            "description": card.get("description", ""),
            "imageUrl": card.get("imageUrl"),
            "imageWidth": 800,
            "imageHeight": 400,
            "link": chat,
        }
    else:
        content = {
            "title": "AI 건강지킴이",
            "description": "받은 건강정보가 믿을 만한지 카카오톡으로 확인해 보세요.",
            "imageUrl": None,
            "link": chat,
        }
    if not content["imageUrl"]:
        content.pop("imageUrl")
        content.pop("imageWidth", None)
        content.pop("imageHeight", None)
    source = (card or {}).get("source")
    if source and secret:
        # 원본 주소는 서명해서 우리 주소에 담는다. 서버를 재시작해도 받은 메시지의 [원본 보기]가 동작한다.
        first = {"title": "원본 보기", "link": _web_link(source_link(base_url, token, source, secret))}
    else:
        first = {"title": "나도 확인해 보기", "link": chat}
    return {
        "objectType": "feed",
        "content": content,
        "buttons": [first, {"title": "채널 홈", "link": home}],
        "serverCallbackArgs": {CALLBACK_KEY: token},
    }


def _web_link(url: str) -> dict:
    return {"mobileWebUrl": url, "webUrl": url}


def _signature(token: str, url: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), f"{token}\n{url}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def source_link(base_url: str, token: str, url: str, secret: str) -> str:
    return f"{base_url.rstrip('/')}{SOURCE_PATH}/{token}?{urlencode({'u': url, 's': _signature(token, url, secret)})}"


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
    await _record(request, "share", ref=token, user_key=(card or {}).get("owner"), outcome=_label_code(card))
    message = _share_message(card, base_url, token, secret=getattr(request.app.state, "kakao_admin_key", None))
    page = _SHARE_HTML.format(
        style=_STYLE.format(font=_FONT),
        sdk=SDK_URL,
        integrity=SDK_INTEGRITY,
        preview=_preview_html(card),
        message=_script_json(message),
        sent_url=_script_json(f"{SHARE_PATH}/{token}/sent"),
        opened_at=int(time.time() * 1000),
        js_key=_script_json(js_key),
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@router.get(SHARE_PATH + "/{token}/sent", include_in_schema=False)
async def share_sent(token: str, after: int = 0) -> Response:
    """공유 페이지가 묻는다. 페이지를 연 뒤에 전송 알림을 받았으면 sent=true."""
    sent_at = _SENT_AT.get(token) if _TOKEN.match(token) else None
    return JSONResponse({"sent": bool(sent_at and sent_at * 1000 >= after)}, headers={"Cache-Control": "no-store"})


_FALLBACK_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>AI 건강지킴이</title></head>
<body style="font-family:sans-serif;padding:32px;font-size:22px;line-height:1.6">
<p><b>AI 건강지킴이</b></p>
<p>받은 건강정보가 믿을 만한지 확인해 드려요.</p>
<p>카카오톡에서 <b>AI 건강지킴이</b> 채널을 검색해 추가하고, 링크나 문장을 보내 주세요.</p>
</body></html>"""


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
    valid = token if isinstance(token, str) and _TOKEN.match(token) else None
    if valid:
        _SENT_AT[valid] = time.time()
        if len(_SENT_AT) > ShareCards.MAX_ITEMS:
            _SENT_AT.pop(next(iter(_SENT_AT)))
    chat = params.get("HASH_CHAT_ID")
    await _record(
        request,
        "share_sent",
        ref=valid,
        outcome=str(params.get("CHAT_TYPE") or "") or None,
        # 채팅방 해시는 한 번 더 해시해 서로 다른 방 수만 셀 수 있게 한다.
        site=hashlib.sha256(str(chat).encode("utf-8")).hexdigest()[:16] if chat else None,
    )
    return JSONResponse({"ok": True})


@router.get(OPEN_PATH + "/{token}", include_in_schema=False)
async def share_open(token: str, request: Request, to: str = "chat") -> Response:
    """공유된 메시지(또는 결과 카드 사진)를 누르면 들어온다. 열린 것으로 기록하고 채널로 보낸다.

    to=home이면 채널 홈, 아니면 채널 채팅방으로 보낸다.
    """
    if not _TOKEN.match(token):
        return HTMLResponse(_FALLBACK_HTML, status_code=404)
    target = "home" if to == "home" else "chat"
    await _record(request, "share_open", ref=token, outcome=target)
    channel_url = getattr(request.app.state, "channel_url", None)
    if channel_url:
        return RedirectResponse(channel_url if target == "home" else f"{channel_url}/chat", status_code=302)
    return HTMLResponse(_FALLBACK_HTML)


@router.get(SOURCE_PATH + "/{token}", include_in_schema=False)
async def share_source(token: str, request: Request, u: str = "", s: str = "") -> Response:
    """공유된 메시지의 [원본 보기]. 서명이 맞는 원본 주소로만 보낸다(아무 주소로나 보내는 통로가 되지 않게)."""
    secret = getattr(request.app.state, "kakao_admin_key", None)
    if not _TOKEN.match(token) or not secret or not _is_web_url(u) or not hmac.compare_digest(s, _signature(token, u, secret)):
        return HTMLResponse(_FALLBACK_HTML, status_code=404)
    await _record(request, "share_open", ref=token, outcome="source")
    return RedirectResponse(u, status_code=302)


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
            event = replace(event, site=site)
        await events.record(event)
    except Exception:
        logger.exception("공유 기록 실패 kind=%s", kind)


__all__ = ["SHARE_CARDS", "ShareCards", "router", "source_link", "with_share_link"]
