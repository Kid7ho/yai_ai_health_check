"""의료 자문 시트와 팀원 분류 시트를 서버에서 연다.

/labeling/{토큰}/ 은 의료 자문 시트(labeling/advisory_sheet.html)다. 교수님이 사례 20건에서 서비스가
뽑은 주장·식약처 정보·논문 분류·신호등이 적절한지 고른다(2026-10-01, 팀원 분류 대신 자문을 받기로 함).
/labeling/{토큰}/team/ 은 이전 팀원 분류 시트(labeling/claim_labeling.html)다. 이미 저장한 분류를 볼 수 있게 남긴다.
두 시트는 같은 저장소를 쓰고 키의 번호로 나뉜다(팀원 m1~m5, 자문위원 a1~a3).
결과는 SQLite 파일 하나(볼륨)에 둔다. 서비스 판정과는 무관한 내부 도구다.

LABELING_TOKEN이 없으면 라우터를 붙이지 않는다.
로그인 기능을 따로 만들지 않고 스킬 경로처럼 추측할 수 없는 주소로 막는다.
토큰 비교는 hmac.compare_digest로 한다.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from healthcheck.config import PROJECT_ROOT

ADVISORY_PATH = PROJECT_ROOT / "labeling" / "advisory_sheet.html"
SHEET_PATH = PROJECT_ROOT / "labeling" / "claim_labeling.html"
SLOTS = frozenset({"m1", "m2", "m3", "m4", "m5", "a1", "a2", "a3"})
# 사례 ID와 번호로 만든 키. 예: lab-001__m1(팀원), link-03__a1(자문위원)
_LABEL_KEY = re.compile(r"^[a-z]+-\d{2,4}__(m[1-5]|a[1-3])$")
MAX_BODY_BYTES = 64 * 1024


class LabelingStore:
    """분류 결과 저장소. 문서 두 종류(분류, 팀원 이름)를 JSON 그대로 둔다."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        with closing(self._connect()) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS labels (key TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS members (slot TEXT PRIMARY KEY, name TEXT NOT NULL, updated_at TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def state(self) -> dict:
        with closing(self._connect()) as conn:
            labels = {key: json.loads(body) for key, body in conn.execute("SELECT key, body FROM labels")}
            members = {slot: {"name": name} for slot, name in conn.execute("SELECT slot, name FROM members")}
        return {"labels": labels, "members": members}

    async def put_label(self, key: str, body: dict) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, "labels", "key", key, "body", json.dumps(body, ensure_ascii=False))

    async def put_member(self, slot: str, name: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, "members", "slot", slot, "name", name)

    def _write(self, table: str, key_column: str, key: str, value_column: str, value: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        # closing은 연결을 닫고, 안쪽 conn은 트랜잭션을 커밋한다.
        with closing(self._connect()) as conn, conn:
            conn.execute(
                f"INSERT INTO {table} ({key_column}, {value_column}, updated_at) VALUES (?, ?, ?) "
                f"ON CONFLICT({key_column}) DO UPDATE SET {value_column}=excluded.{value_column}, updated_at=excluded.updated_at",
                (key, value, now),
            )


def build_router(
    token: str,
    store: LabelingStore,
    sheet_path: Path = SHEET_PATH,
    advisory_path: Path = ADVISORY_PATH,
) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    def check(given: str) -> None:
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(status_code=404)

    async def json_body(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="분류 내용이 너무 큽니다")
        try:
            body = json.loads(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="JSON이 아닙니다") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="객체여야 합니다")
        return body

    def serve(given: str, path: Path) -> HTMLResponse:
        check(given)
        if not path.is_file():
            raise HTTPException(status_code=503, detail="시트 파일이 없습니다")
        html = path.read_text(encoding="utf-8")
        # 시트가 서버 저장소를 쓰도록 주소를 알려 준다. 토큰은 이미 주소에 있으므로 새로 드러나는 값은 없다.
        inject = (
            '<!doctype html><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            f'<script>window.LABELING_API = "/labeling/{given}/api";</script>\n'
        )
        return HTMLResponse(
            inject + html,
            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer"},
        )

    @router.get("/labeling/{given}")
    async def redirect_slash(given: str) -> RedirectResponse:
        check(given)
        return RedirectResponse(f"/labeling/{given}/", status_code=307)

    @router.get("/labeling/{given}/")
    async def advisory(given: str) -> HTMLResponse:
        return serve(given, advisory_path)

    @router.get("/labeling/{given}/team")
    async def redirect_team_slash(given: str) -> RedirectResponse:
        check(given)
        return RedirectResponse(f"/labeling/{given}/team/", status_code=307)

    @router.get("/labeling/{given}/team/")
    async def team_sheet(given: str) -> HTMLResponse:
        return serve(given, sheet_path)

    @router.get("/labeling/{given}/api/state")
    async def state(given: str) -> JSONResponse:
        check(given)
        return JSONResponse(await asyncio.to_thread(store.state), headers={"Cache-Control": "no-store"})

    @router.put("/labeling/{given}/api/labels/{key}")
    async def put_label(given: str, key: str, request: Request) -> dict:
        check(given)
        if not _LABEL_KEY.match(key):
            raise HTTPException(status_code=400, detail="분류 키 형식이 아닙니다")
        body = await json_body(request)
        if body.get("slot") != key.rsplit("__", 1)[1]:
            raise HTTPException(status_code=400, detail="번호가 키와 다릅니다")
        await store.put_label(key, body)
        return {"ok": True}

    @router.put("/labeling/{given}/api/members/{slot}")
    async def put_member(given: str, slot: str, request: Request) -> dict:
        check(given)
        if slot not in SLOTS:
            raise HTTPException(status_code=400, detail="번호가 아닙니다")
        name = str((await json_body(request)).get("name") or "").strip()[:40]
        if not name:
            raise HTTPException(status_code=400, detail="이름이 비어 있습니다")
        await store.put_member(slot, name)
        return {"ok": True}

    return router
