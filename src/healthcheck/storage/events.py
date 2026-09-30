"""응답 기록 저장소와 기록을 만드는 도우미.

판정 DB와 같은 SQLite 파일에 events 표를 둔다. 보낸 글 내용은 담지 않는다.
사용자 키는 처음부터 해시로 저장한다. 판정 기록은 사용자 키를 30일 뒤 해시로 바꾸는데,
같은 해시 방식을 써야 기간이 지나도 같은 사람으로 이어서 셀 수 있다.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from healthcheck.domain.types import CheckResult, ShareInput
from healthcheck.ports.events import Event

# 판정 기록 원문 보관 기간과 같다.
RETENTION_DAYS = 90

# 워커가 파이프라인 실패 시 쓰는 규칙 ID(worker._ERROR_RULE_ID)
_ERROR_RULE_ID = "ERR-PIPELINE"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    user_hash   TEXT,
    kind        TEXT NOT NULL,
    outcome     TEXT,
    input_kind  TEXT,
    input_form  TEXT,
    site        TEXT,
    via         TEXT,
    latency_ms  INTEGER,
    delivered   INTEGER,
    ref         TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_at);
CREATE INDEX IF NOT EXISTS idx_events_user ON events (user_hash, created_at);
"""
# 2026-09-30 배포한 첫 표에는 ref가 없다. 있는 표에 칸만 더한다.
_MIGRATIONS = {"ref": "ALTER TABLE events ADD COLUMN ref TEXT"}


def user_hash(user_key: str | None) -> str | None:
    """카카오 사용자 키를 익명 번호로 바꾼다. 판정 기록의 만료 해시(sqlite_repo._purge)와 같은 방식이다."""
    if not user_key:
        return None
    return hashlib.sha256(user_key.encode("utf-8")).hexdigest()[:32]


def outcome_of(result: CheckResult) -> str:
    """확인 요청에 어떤 답이 나갔는지. step6_response.render()의 분기 순서와 같다."""
    if result.retryable_failure:
        return "retry"
    if result.notice:
        # login_required:네이버 카페 → notice:login_required. 사이트 이름은 site 칸에 따로 있다.
        return f"notice:{result.notice.split(':', 1)[0]}"
    if result.verdict.rule_id == _ERROR_RULE_ID:
        return "error"
    if result.claims is not None and not result.claims.in_scope:
        return "out_of_scope"
    return result.verdict.label.value


def describe_input(share_input: ShareInput) -> tuple[str, str | None]:
    """입력 형태와 사이트 이름. 글 내용은 돌려주지 않는다.

    형태: video · shorts · youtube_other · link · short_link · short_text · text · long_text
    링크에 다른 글이 섞여 있으면 뒤에 +text를 붙인다(공유 문구·인사말이 함께 온 경우).
    """
    url = share_input.url
    site = _site(url)
    if share_input.kind == "PLAIN_TEXT":
        length = len(share_input.raw_text.strip())
        form = "short_text" if length < 30 else "text" if length < 150 else "long_text"
        return form, None
    if share_input.needs_resolution:
        form = "short_link"
    elif share_input.kind == "YOUTUBE":
        form = "shorts" if url and "/shorts/" in url else "video"
    elif share_input.kind == "YOUTUBE_OTHER":
        form = "youtube_other"
    else:
        form = "link"
    rest = share_input.raw_text.replace(url, "") if url else share_input.raw_text
    if rest.strip():
        form += "+text"
    return form, site


def _site(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = (urlsplit(url if "://" in url else f"https://{url}").hostname or "").lower()
    except ValueError:
        return None
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    if host in ("youtu.be", "youtube-nocookie.com"):
        host = "youtube.com"
    return host or None


def new_event(
    kind: str,
    *,
    user_key: str | None,
    outcome: str | None = None,
    share_input: ShareInput | None = None,
    via: str | None = None,
    latency_ms: int | None = None,
    delivered: bool | None = None,
    ref: str | None = None,
    event_id: str | None = None,
    now: datetime | None = None,
) -> Event:
    form, site = describe_input(share_input) if share_input is not None else (None, None)
    return Event(
        event_id=event_id or uuid.uuid4().hex,
        created_at=now or datetime.now(timezone.utc),
        user_hash=user_hash(user_key),
        kind=kind,
        outcome=outcome,
        input_kind=share_input.kind if share_input is not None else None,
        input_form=form,
        site=site,
        via=via,
        latency_ms=latency_ms,
        delivered=delivered,
        ref=ref,
    )


class SqliteEventLog:
    def __init__(self, path: Path | str, *, retention_days: int = RETENTION_DAYS) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._shared = sqlite3.connect(self._path, check_same_thread=False) if self._path == ":memory:" else None
        self._retention = timedelta(days=retention_days)
        self._lock = asyncio.Lock()
        connection = self._connect()
        try:
            connection.executescript(_SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
            for column, statement in _MIGRATIONS.items():
                if column not in columns:
                    connection.execute(statement)
            connection.commit()
        finally:
            self._close(connection)

    def _connect(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        connection = sqlite3.connect(self._path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _close(self, connection: sqlite3.Connection) -> None:
        if self._shared is None:
            connection.close()

    async def record(self, event: Event) -> None:
        async with self._lock:
            await asyncio.to_thread(self._insert, event)

    async def list_events(self) -> list[Event]:
        return await asyncio.to_thread(read_events, self._connect, self._close)

    async def purge_expired(self, now: datetime | None = None) -> int:
        cutoff = (now or datetime.now(timezone.utc)) - self._retention
        async with self._lock:
            return await asyncio.to_thread(self._delete_before, cutoff)

    def _insert(self, event: Event) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO events (event_id, created_at, user_hash, kind, outcome, input_kind, input_form,"
                " site, via, latency_ms, delivered, ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.created_at.isoformat(),
                    event.user_hash,
                    event.kind,
                    event.outcome,
                    event.input_kind,
                    event.input_form,
                    event.site,
                    event.via,
                    event.latency_ms,
                    None if event.delivered is None else int(event.delivered),
                    event.ref,
                ),
            )
            connection.commit()
        finally:
            self._close(connection)

    def _delete_before(self, cutoff: datetime) -> int:
        connection = self._connect()
        try:
            cursor = connection.execute("DELETE FROM events WHERE created_at < ?", (cutoff.isoformat(),))
            connection.commit()
            return cursor.rowcount
        finally:
            self._close(connection)


def read_events(connect, close=lambda connection: connection.close()) -> list[Event]:
    """events 표를 시간순으로 읽는다. 집계 스크립트도 이 함수를 쓴다."""
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT event_id, created_at, user_hash, kind, outcome, input_kind, input_form, site, via,"
            " latency_ms, delivered, ref FROM events ORDER BY created_at"
        ).fetchall()
    finally:
        close(connection)
    return [
        Event(
            event_id=row[0],
            created_at=datetime.fromisoformat(row[1]),
            user_hash=row[2],
            kind=row[3],
            outcome=row[4],
            input_kind=row[5],
            input_form=row[6],
            site=row[7],
            via=row[8],
            latency_ms=row[9],
            delivered=None if row[10] is None else bool(row[10]),
            ref=row[11],
        )
        for row in rows
    ]


class InMemoryEventLog:
    """테스트와 fake 모드용."""

    def __init__(self, *, retention_days: int = RETENTION_DAYS) -> None:
        self.events: list[Event] = []
        self._retention = timedelta(days=retention_days)

    async def record(self, event: Event) -> None:
        self.events.append(event)

    async def list_events(self) -> list[Event]:
        return sorted(self.events, key=lambda event: event.created_at)

    async def purge_expired(self, now: datetime | None = None) -> int:
        cutoff = (now or datetime.now(timezone.utc)) - self._retention
        before = len(self.events)
        self.events = [event for event in self.events if event.created_at >= cutoff]
        return before - len(self.events)


__all__ = [
    "InMemoryEventLog",
    "RETENTION_DAYS",
    "SqliteEventLog",
    "describe_input",
    "new_event",
    "outcome_of",
    "read_events",
    "user_hash",
]
