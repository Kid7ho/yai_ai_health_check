"""식약처 개별인정형 원료(I-0040) 로컬 사본.

식품안전나라 오픈API는 09시~19시(한국 시간)에 "ERROR-503: 09시~19시에는 서비스가
제한됩니다"를 돌려준다(2026-09-15 실측). 어르신이 건강정보를 주고받는 낮 시간에
식약처 확인이 전부 실패해 인정 원료 주장이 '알 수 없어요'로 떨어졌다.

I-0040은 전체 773건으로 작다. 제한이 없는 시간에 전체를 내려받아 파일로 두고,
조회는 파일에서 한다. 서버가 켜져 있는 동안 주기적으로 확인해 하루가 지난 사본을
제한 없는 시간에 새로 받는다. 받는 도중 실패하면 이전 사본을 그대로 쓴다.

부분 검색은 API의 APLC_RAWMTRL_NM 조건과 같게 '신청원료명에 포함'으로 본다.
API 실측에서 "마늘"이 "흑마늘추출물"에 걸린 것과 같은 동작이다.
TODO(확인필요): API 부분 검색이 공백·대소문자를 무시하는지는 확인하지 못했다.
제한 없는 시간에 성분 38종을 API와 사본으로 함께 조회해 결과가 같은지 확인한다.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
# API가 막히는 시간. 시작 시각은 포함하고 끝 시각은 포함하지 않는다.
RESTRICTED_START_HOUR = 9
RESTRICTED_END_HOUR = 19

PAGE_SIZE = 100
# 이 시간이 지나면 새로 받는다. 인정 현황은 자주 바뀌지 않는다.
MAX_AGE = timedelta(hours=24)
SNAPSHOT_FORMAT = 1


class SnapshotError(RuntimeError):
    """사본을 받거나 읽지 못했다. 이전 사본은 그대로 둔다."""


def in_restricted_hours(now: datetime | None = None) -> bool:
    """지금이 API 제한 시간(한국 시간 09시~19시)인가."""
    now = (now or datetime.now(timezone.utc)).astimezone(KST)
    return RESTRICTED_START_HOUR <= now.hour < RESTRICTED_END_HOUR


@dataclass(frozen=True, slots=True)
class Snapshot:
    fetched_at: datetime
    rows: tuple[dict, ...]

    def search(self, ingredient: str) -> list[dict]:
        """신청원료명에 성분명이 들어간 인정 건. API 부분 검색과 같은 규칙이다."""
        return [row for row in self.rows if ingredient in str(row.get("APLC_RAWMTRL_NM") or "")]

    def age(self, now: datetime | None = None) -> timedelta:
        return (now or datetime.now(timezone.utc)) - self.fetched_at

    def is_stale(self, now: datetime | None = None) -> bool:
        return self.age(now) >= MAX_AGE


def load_snapshot(path: Path | str) -> Snapshot | None:
    """파일에서 사본을 읽는다. 없거나 깨졌으면 None이다."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("format") != SNAPSHOT_FORMAT:
            raise ValueError(f"형식 버전이 다르다: {raw.get('format')!r}")
        rows = tuple(row for row in raw["rows"] if isinstance(row, dict))
        if len(rows) != int(raw["total_count"]):
            raise ValueError("건수가 맞지 않는다")
        fetched_at = datetime.fromisoformat(raw["fetched_at"])
    except (ValueError, KeyError, TypeError) as error:
        logger.warning("식약처 사본을 읽지 못해 쓰지 않는다 path=%s: %s", path, error)
        return None
    return Snapshot(fetched_at=fetched_at, rows=rows)


def save_snapshot(path: Path | str, snapshot: Snapshot) -> None:
    """다 쓴 뒤에 이름을 바꾼다. 쓰는 도중 꺼져도 이전 사본이 깨지지 않는다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": SNAPSHOT_FORMAT,
        "service": "I-0040",
        "fetched_at": snapshot.fetched_at.isoformat(),
        "total_count": len(snapshot.rows),
        "rows": list(snapshot.rows),
    }
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".mfds-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


async def download_snapshot(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    base_url: str,
    service_id: str = "I-0040",
    timeout: float = 15.0,
    now: datetime | None = None,
) -> Snapshot:
    """전체 인정 건을 쪽 단위로 받는다. 받은 건수가 전체 건수와 다르면 버린다."""
    rows: list[dict] = []
    total: int | None = None
    start = 1

    while total is None or start <= total:
        end = start + PAGE_SIZE - 1
        url = f"{base_url.rstrip('/')}/{api_key}/{service_id}/json/{start}/{end}"
        try:
            response = await client.get(url, timeout=timeout)
        except httpx.HTTPError as error:
            raise SnapshotError(f"식약처 통신 오류: {type(error).__name__}") from error
        if response.status_code != 200:
            raise SnapshotError(f"식약처 응답 {response.status_code}")
        try:
            payload = response.json()[service_id]
        except (ValueError, KeyError, TypeError) as error:
            raise SnapshotError("식약처 응답 형식이 예상과 다르다") from error

        result = payload.get("RESULT") or {}
        code = result.get("CODE", "")
        if code and code != "INFO-000":
            raise SnapshotError(f"식약처 오류 {code}: {result.get('MSG', '')}")

        if total is None:
            try:
                total = int(payload.get("total_count"))
            except (TypeError, ValueError) as error:
                raise SnapshotError("전체 건수를 알 수 없다") from error
            if total <= 0:
                raise SnapshotError("전체 건수가 0이다")

        page = [row for row in payload.get("row") or [] if isinstance(row, dict)]
        if not page:
            break
        rows.extend(page)
        start = end + 1

    if len(rows) != total:
        raise SnapshotError(f"받은 건수 {len(rows)}가 전체 {total}와 다르다")

    return Snapshot(fetched_at=now or datetime.now(timezone.utc), rows=tuple(rows))
