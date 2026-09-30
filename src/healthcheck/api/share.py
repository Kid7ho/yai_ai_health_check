"""공유된 결과 카드가 열린 수를 센다.

카카오 공유 버튼(share)은 누른 사실이 서버로 오지 않는다. 버튼을 서버 경유로 바꾸면 어르신이 한 번 더
눌러야 해서 되돌렸다(2026-09-30). 대신 결과 카드 사진에 우리 주소를 걸어 둔다. 공유받은 사람이 사진을
누르면 이 경로로 들어오고, 어느 결과 카드였는지(ref = 그 확인 기록의 event_id)를 남긴 뒤 채널로 보낸다.

한계: 사진을 누른 사람이 공유받은 사람인지 결과를 받은 본인인지 구분하지 못한다. 공유 클릭 수가 아니라
"공유된 카드가 새 사람을 데려온 수"에 가깝다. 공유 여부 자체는 설문으로 잰다.
TODO(확인필요): 공유된 카드에서도 사진 링크가 그대로 동작하는지 휴대폰으로 확인한다.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from healthcheck.storage.events import new_event

logger = logging.getLogger(__name__)

SHARE_PATH = "/s"
_TOKEN = re.compile(r"^[0-9a-f]{32}$")

router = APIRouter()

# 채널 주소를 모를 때 보여 주는 안내. 판정 결과가 아니라서 면책 문구를 붙이지 않는다.
_FALLBACK_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>AI 건강지킴이</title></head>
<body style="font-family:sans-serif;padding:32px;font-size:22px;line-height:1.6">
<p><b>AI 건강지킴이</b></p>
<p>받은 건강정보가 믿을 만한지 확인해 드려요.</p>
<p>카카오톡에서 <b>AI 건강지킴이</b> 채널을 검색해 추가하고, 링크나 문장을 보내 주세요.</p>
</body></html>"""


def share_link(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}{SHARE_PATH}/{token}"


def with_share_link(payload: dict, base_url: str | None, token: str) -> dict:
    """공유 버튼이 달린 결과 카드의 사진에 추적 주소를 건다. 카드 문구는 바꾸지 않는다."""
    if not base_url:
        return payload
    for output in payload.get("template", {}).get("outputs", []):
        card = output.get("basicCard")
        if not card or "thumbnail" not in card:
            continue
        if not any(button.get("action") == "share" for button in card.get("buttons", [])):
            continue
        card["thumbnail"]["link"] = {"web": share_link(base_url, token)}
    return payload


@router.get(SHARE_PATH + "/{token}", include_in_schema=False)
async def share_open(token: str, request: Request) -> Response:
    if not _TOKEN.match(token):
        return HTMLResponse(_FALLBACK_HTML, status_code=404)
    events = getattr(request.app.state, "events", None)
    if events is not None:
        try:
            await events.record(new_event("share_open", user_key=None, ref=token))
        except Exception:
            logger.exception("공유 카드 열림 기록 실패")
    logger.info("공유된 결과 카드가 열렸다")
    channel_url = getattr(request.app.state, "channel_url", None)
    if channel_url:
        return RedirectResponse(channel_url, status_code=302)
    return HTMLResponse(_FALLBACK_HTML)
