"""영상 받아 적기 캐시 — 같은 영상을 다시 읽지 않는다.

도쿄 서버에서는 유튜브 자막이 막혀 모든 영상을 Gemini가 받아 적는다. 무료 등급 한도는 하루 영상
8시간 분량이라, 같은 영상을 다시 읽으면 그만큼 한도가 준다. 경로당·복지관에서는 같은 영상이
여러 어르신에게 돌기 때문에 중복이 많다.

판정 캐시와 추출 캐시는 메모리에 있어 배포·재시작 때마다 비었다. 이 캐시는 볼륨의 SQLite에 두어
재시작과 규칙표 버전 변경을 넘어 남는다. 담는 것은 공개 영상의 받아 적은 글과 제목뿐이다.

제목만으로 만든 본문(title_only)은 담지 않는다. 자막이나 Gemini가 잠시 실패했을 때 나오는 얕은
결과이고, 이것을 담으면 다음에도 계속 얕은 본문을 쓰게 된다.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from healthcheck.domain.types import ExtractedContent
from healthcheck.ports.transcript import TranscriptExtractor

logger = logging.getLogger(__name__)

# 보관 기간. 영상 내용은 잘 바뀌지 않지만, 지워진 영상의 글이 오래 남지 않게 한다.
RETENTION_DAYS = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    video_id        TEXT PRIMARY KEY,
    text            TEXT NOT NULL,
    confidence      REAL NOT NULL,
    language        TEXT,
    content_hash    TEXT,
    source          TEXT,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);
"""


class SqliteTranscriptCache:
    """video_id → 받아 적은 본문."""

    def __init__(self, path: Path | str, *, retention_days: int = RETENTION_DAYS) -> None:
        self._path = Path(path)
        self._retention = timedelta(days=retention_days)
        self._lock = asyncio.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
        self.hits = 0
        self.misses = 0

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=5)

    async def get(self, video_id: str) -> ExtractedContent | None:
        return await asyncio.to_thread(self._get, video_id, datetime.now(timezone.utc))

    async def set(self, video_id: str, content: ExtractedContent) -> None:
        async with self._lock:
            await asyncio.to_thread(self._set, video_id, content, datetime.now(timezone.utc))

    async def purge_expired(self, now: datetime | None = None) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._purge, now or datetime.now(timezone.utc))

    def _get(self, video_id: str, now: datetime) -> ExtractedContent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT text, confidence, language, content_hash, source FROM transcripts"
                " WHERE video_id = ? AND expires_at > ?",
                (video_id, now.isoformat()),
            ).fetchone()
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        return ExtractedContent(
            text=row[0],
            has_transcript=True,
            confidence=row[1],
            language=row[2],
            content_hash=row[3],
            source=row[4],
        )

    def _set(self, video_id: str, content: ExtractedContent, now: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO transcripts"
                " (video_id, text, confidence, language, content_hash, source, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    video_id,
                    content.text,
                    content.confidence,
                    content.language,
                    content.content_hash,
                    content.source,
                    now.isoformat(),
                    (now + self._retention).isoformat(),
                ),
            )

    def _purge(self, now: datetime) -> int:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM transcripts WHERE expires_at <= ?", (now.isoformat(),))
            return cursor.rowcount


class CachingTranscriptExtractor:
    """받아 적기 결과를 캐시에 담고 꺼내 쓰는 추출기.

    TranscriptExtractor 인터페이스를 그대로 만족하므로 파이프라인은 캐시가 붙었는지 모른다.
    실패(예외)는 담지 않는다. 일시적 실패를 담으면 그 상태가 굳는다.
    """

    def __init__(self, inner: TranscriptExtractor, cache: SqliteTranscriptCache) -> None:
        self._inner = inner
        self._cache = cache

    async def fetch(self, video_id: str) -> ExtractedContent:
        cached = await self._cache.get(video_id)
        if cached is not None:
            logger.info("영상 받아 적기 캐시 적중 video=%s source=%s", video_id, cached.source)
            return cached

        content = await self._inner.fetch(video_id)
        if content.has_transcript and content.text.strip() and content.source != "title_only":
            await self._cache.set(video_id, content)
        return content

    def __getattr__(self, name: str):
        # is_blocked 같은 안쪽 추출기의 상태를 그대로 보여 준다(/health가 읽는다).
        return getattr(self._inner, name)
