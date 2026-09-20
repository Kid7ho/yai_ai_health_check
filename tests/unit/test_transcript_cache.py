"""영상 받아 적기 캐시 테스트. 네트워크를 타지 않는다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from healthcheck.domain.types import ExtractedContent
from healthcheck.storage.transcript_cache import CachingTranscriptExtractor, SqliteTranscriptCache


class CountingExtractor:
    """부른 횟수를 센다. is_blocked 같은 상태도 그대로 보여 주는지 확인한다."""

    is_blocked = False

    def __init__(self, content: ExtractedContent | None = None, error: Exception | None = None) -> None:
        self._content = content
        self._error = error
        self.calls = 0

    async def fetch(self, video_id: str) -> ExtractedContent:
        self.calls += 1
        if self._error:
            raise self._error
        return self._content


def content(text: str = "홍삼이 면역력에 좋습니다", source: str = "gemini") -> ExtractedContent:
    return ExtractedContent(text=text, has_transcript=True, confidence=0.75, source=source, content_hash="h1")


@pytest.fixture
def cache(tmp_path):
    return SqliteTranscriptCache(tmp_path / "transcripts.db")


async def test_같은_영상은_한_번만_읽는다(cache):
    """Gemini 무료 한도를 아끼는 핵심이다."""
    inner = CountingExtractor(content())
    extractor = CachingTranscriptExtractor(inner, cache)

    first = await extractor.fetch("abc123")
    second = await extractor.fetch("abc123")

    assert inner.calls == 1
    assert (second.text, second.source, second.confidence) == (first.text, first.source, first.confidence)
    assert (cache.hits, cache.misses) == (1, 1)


async def test_다시_만든_캐시도_남아_있다(tmp_path):
    """배포·재시작을 넘어 남아야 한다. 메모리 캐시는 그때마다 비었다."""
    inner = CountingExtractor(content())
    await CachingTranscriptExtractor(inner, SqliteTranscriptCache(tmp_path / "t.db")).fetch("abc123")
    again = await CachingTranscriptExtractor(inner, SqliteTranscriptCache(tmp_path / "t.db")).fetch("abc123")

    assert inner.calls == 1
    assert again.text == "홍삼이 면역력에 좋습니다"


async def test_제목만으로_만든_본문은_담지_않는다(cache):
    """자막과 Gemini가 잠시 실패했을 때 나오는 얕은 결과다. 담으면 그 상태가 굳는다."""
    inner = CountingExtractor(content(source="title_only"))
    extractor = CachingTranscriptExtractor(inner, cache)

    await extractor.fetch("abc123")
    await extractor.fetch("abc123")

    assert inner.calls == 2


async def test_실패는_담지_않는다(cache):
    inner = CountingExtractor(error=RuntimeError("Gemini 한도 초과"))
    extractor = CachingTranscriptExtractor(inner, cache)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await extractor.fetch("abc123")
    assert inner.calls == 2


async def test_보관_기간이_지나면_다시_읽는다(tmp_path):
    cache = SqliteTranscriptCache(tmp_path / "t.db", retention_days=0)
    inner = CountingExtractor(content())
    extractor = CachingTranscriptExtractor(inner, cache)

    await extractor.fetch("abc123")
    await extractor.fetch("abc123")

    assert inner.calls == 2
    assert await cache.purge_expired(datetime.now(timezone.utc) + timedelta(seconds=1)) >= 1


async def test_안쪽_추출기의_상태를_그대로_보여_준다(cache):
    inner = CountingExtractor(content())
    inner.is_blocked = True
    assert CachingTranscriptExtractor(inner, cache).is_blocked is True
