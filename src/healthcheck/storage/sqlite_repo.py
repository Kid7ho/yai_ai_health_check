"""SQLite 저장소.

표준 라이브러리 sqlite3를 스레드로 넘겨 쓴다. aiosqlite를 넣지 않은 이유는
의존성을 하나 더 늘릴 만큼 얻는 것이 없기 때문이다. 쓰기 빈도가 낮고
한 요청에 한 번 저장한다.

Postgres로 바꿀 때는 CheckRepository를 만족하는 구현을 새로 만들면 된다.
파이프라인 코드는 이 클래스를 직접 참조하지 않는다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from healthcheck.domain.labels import Label
from healthcheck.ports.repository import CheckRecord, Metrics

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    check_id            TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    label               TEXT NOT NULL,
    rule_id             TEXT NOT NULL,
    rules_version       TEXT NOT NULL,
    input_kind          TEXT NOT NULL,
    from_cache          INTEGER NOT NULL DEFAULT 0,
    ingredient          TEXT,
    claimed_effect      TEXT,
    intensity           TEXT,
    recognition         TEXT,
    supporting_papers   INTEGER NOT NULL DEFAULT 0,
    refuting_papers     INTEGER NOT NULL DEFAULT 0,
    transcript_quality  TEXT,
    raw_text            TEXT,
    transcript_text     TEXT,
    user_key            TEXT,
    user_key_hashed     INTEGER NOT NULL DEFAULT 0,
    video_id            TEXT,
    url                 TEXT,
    content_hash        TEXT,
    text_expires_at     TEXT,
    user_key_expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_created ON checks (created_at);
CREATE INDEX IF NOT EXISTS idx_checks_label ON checks (label, rule_id);
-- 보존 기간이 지난 항목을 찾는 데 쓴다.
CREATE INDEX IF NOT EXISTS idx_checks_text_expiry ON checks (text_expires_at);
CREATE INDEX IF NOT EXISTS idx_checks_user_expiry ON checks (user_key_expires_at);
"""


class SqliteCheckRepository:
    def __init__(self, path: Path | str = "healthcheck.db") -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        # :memory:는 연결마다 별개 DB가 되므로 연결을 붙들고 쓴다.
        self._shared = sqlite3.connect(self._path, check_same_thread=False) if self._path == ":memory:" else None
        self._lock = asyncio.Lock()
        self._migrate()

    # ── 동기 구간 ────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        connection = sqlite3.connect(self._path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _migrate(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(SCHEMA)
            connection.commit()
        finally:
            if self._shared is None:
                connection.close()

    def _save(self, record: CheckRecord) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT OR REPLACE INTO checks (
                    check_id, created_at, label, rule_id, rules_version, input_kind,
                    from_cache, ingredient, claimed_effect, intensity, recognition,
                    supporting_papers, refuting_papers, transcript_quality,
                    raw_text, transcript_text, user_key, video_id, url, content_hash,
                    text_expires_at, user_key_expires_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.check_id,
                    record.created_at.isoformat(),
                    record.label.value,
                    record.rule_id,
                    record.rules_version,
                    record.input_kind,
                    int(record.from_cache),
                    record.ingredient,
                    record.claimed_effect,
                    record.intensity,
                    record.recognition,
                    record.supporting_papers,
                    record.refuting_papers,
                    record.transcript_quality,
                    record.raw_text,
                    record.transcript_text,
                    record.user_key,
                    record.video_id,
                    record.url,
                    record.content_hash,
                    record.text_expires_at.isoformat() if record.text_expires_at else None,
                    record.user_key_expires_at.isoformat() if record.user_key_expires_at else None,
                ),
            )
            connection.commit()
        finally:
            if self._shared is None:
                connection.close()

    def _metrics(self) -> Metrics:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            total = cursor.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
            by_label = {
                row[0]: row[1]
                for row in cursor.execute(
                    "SELECT label, COUNT(*) FROM checks GROUP BY label"
                ).fetchall()
            }
            # 오정보 유형 수. 주의·조심 판정의 (규칙 ID, 성분) 조합 가짓수다.
            types = cursor.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT DISTINCT rule_id, ingredient FROM checks
                    WHERE label IN (?, ?) AND ingredient IS NOT NULL
                )
                """,
                (Label.WARNING.value, Label.CAUTION.value),
            ).fetchone()[0]
            cache_hits = cursor.execute(
                "SELECT COUNT(*) FROM checks WHERE from_cache = 1"
            ).fetchone()[0]
            users = cursor.execute(
                "SELECT COUNT(DISTINCT user_key) FROM checks WHERE user_key IS NOT NULL"
            ).fetchone()[0]
            versions = {
                row[0]: row[1]
                for row in cursor.execute(
                    "SELECT rules_version, COUNT(*) FROM checks GROUP BY rules_version"
                ).fetchall()
            }
        finally:
            if self._shared is None:
                connection.close()

        return Metrics(
            total_checks=total,
            by_label=by_label,
            misinformation_types=types,
            cache_hits=cache_hits,
            distinct_users=users,
            rules_versions=versions,
        )

    def _purge(self, now: datetime) -> dict[str, int]:
        stamp = now.isoformat()
        connection = self._connect()
        try:
            cursor = connection.cursor()

            # 원문과 자막은 보존 기간이 지나면 비운다. 행은 남긴다.
            cursor.execute(
                """
                UPDATE checks SET raw_text = NULL, transcript_text = NULL, text_expires_at = NULL
                WHERE text_expires_at IS NOT NULL AND text_expires_at <= ?
                """,
                (stamp,),
            )
            texts = cursor.rowcount

            # 사용자 키는 지우는 대신 해시로 바꾼다. 같은 사람인지는 구분할 수
            # 있지만 누구인지는 알 수 없게 하려는 것이다.
            rows = cursor.execute(
                """
                SELECT check_id, user_key FROM checks
                WHERE user_key IS NOT NULL AND user_key_hashed = 0
                  AND user_key_expires_at IS NOT NULL AND user_key_expires_at <= ?
                """,
                (stamp,),
            ).fetchall()
            for check_id, user_key in rows:
                cursor.execute(
                    """
                    UPDATE checks SET user_key = ?, user_key_hashed = 1, user_key_expires_at = NULL
                    WHERE check_id = ?
                    """,
                    (hashlib.sha256(user_key.encode("utf-8")).hexdigest()[:32], check_id),
                )

            connection.commit()
        finally:
            if self._shared is None:
                connection.close()

        result = {"texts_cleared": texts, "user_keys_hashed": len(rows)}
        if texts or rows:
            logger.info("보존 기간 만료 처리: %s", result)
        return result

    # ── 비동기 표면 ──────────────────────────────────────────

    async def save(self, record: CheckRecord) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save, record)

    async def metrics(self) -> Metrics:
        return await asyncio.to_thread(self._metrics)

    async def purge_expired(self, now: datetime | None = None) -> dict[str, int]:
        async with self._lock:
            return await asyncio.to_thread(self._purge, now or datetime.now(timezone.utc))

    def close(self) -> None:
        if self._shared is not None:
            self._shared.close()
            self._shared = None
