"""포스터 QR — 스캔한 수를 센다.

포스터의 QR은 채널 주소로 바로 가지 않고 /q/{태그}를 거친다. 스캔을 기록한 뒤 채널 홈으로 보낸다.
장소마다 태그를 다르게 뽑으면(poster-경로당A 등) 어느 포스터가 몇 번 스캔됐는지 알 수 있다.
스캔한 사람이 누구인지는 모른다(카카오 사용자 키는 채팅할 때만 온다). 그래서 스캔 수와 그날 새로 온 사용자 수를
함께 본다.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from healthcheck.storage.events import new_event

logger = logging.getLogger(__name__)

POSTER_PATH = "/q"
_TAG = re.compile(r"^[a-z0-9-]{1,40}$")

router = APIRouter()

_FALLBACK_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>AI 건강지킴이</title></head>
<body style="font-family:sans-serif;padding:32px;font-size:22px;line-height:1.6">
<p><b>AI 건강지킴이</b></p>
<p>카카오톡에서 <b>AI 건강지킴이</b> 채널을 검색해 추가하고, 링크나 문장을 보내 주세요.</p>
</body></html>"""


def poster_link(base_url: str, tag: str) -> str:
    return f"{base_url.rstrip('/')}{POSTER_PATH}/{tag}"


@router.get(POSTER_PATH + "/{tag}", include_in_schema=False)
async def poster_scan(tag: str, request: Request) -> Response:
    if not _TAG.match(tag):
        return HTMLResponse(_FALLBACK_HTML, status_code=404)
    events = getattr(request.app.state, "events", None)
    if events is not None:
        try:
            await events.record(new_event("poster_scan", user_key=None, outcome=tag))
        except Exception:
            logger.exception("포스터 스캔 기록 실패")
    channel_url = getattr(request.app.state, "channel_url", None)
    if channel_url:
        return RedirectResponse(channel_url, status_code=302)
    return HTMLResponse(_FALLBACK_HTML)
