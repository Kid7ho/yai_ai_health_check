"""유튜브 영상 제목 붙이기 테스트. 네트워크를 타지 않는다."""

from __future__ import annotations

import httpx
import pytest

from healthcheck.adapters.youtube.titled import (
    TITLE_ONLY_CONFIDENCE,
    TitledTranscriptExtractor,
    YouTubeTitleFetcher,
)
from healthcheck.adapters.youtube.transcript import TranscriptFetchError
from healthcheck.domain.types import ExtractedContent

TITLE = "홍삼 먹으면 당뇨 완치? 의사가 알려주는 진실"


class Inner:
    def __init__(self, content=None, error=None):
        self.content, self.error = content, error

    async def fetch(self, video_id):
        if self.error:
            raise self.error
        return self.content


class Titles:
    def __init__(self, title):
        self._title = title

    async def title(self, video_id):
        return self._title


CAPTION = ExtractedContent(text="홍삼은 면역력에 도움을 줄 수 있습니다", has_transcript=True, confidence=0.65, source="captions")
NONE = ExtractedContent(text="", has_transcript=False, confidence=0.0)


async def test_자막_앞에_제목을_붙인다():
    """건강 오정보는 핵심 주장이 제목에 담기는 경우가 많다."""
    content = await TitledTranscriptExtractor(Inner(CAPTION), Titles(TITLE)).fetch("vid")

    assert content.text.startswith(TITLE + "\n")
    assert "면역력" in content.text
    assert content.confidence == 0.65


async def test_제목이_붙으면_추출_캐시_키도_바뀐다():
    """같은 자막이라도 제목이 다르면 다른 주장이 뽑힐 수 있다."""
    content = await TitledTranscriptExtractor(Inner(CAPTION), Titles(TITLE)).fetch("vid")
    assert content.content_hash != CAPTION.content_hash


async def test_자막이_없으면_제목만으로_본문을_만든다():
    content = await TitledTranscriptExtractor(Inner(NONE), Titles(TITLE)).fetch("vid")

    assert content.has_transcript is True
    assert content.text == TITLE
    assert content.confidence == TITLE_ONLY_CONFIDENCE
    assert content.source == "title_only"


async def test_제목도_자막도_없으면_판정_보류로_간다():
    content = await TitledTranscriptExtractor(Inner(NONE), Titles(None)).fetch("vid")
    assert content.has_transcript is False


async def test_자막_차단은_제목으로_덮지_않고_그대로_올린다():
    """제목만으로 답하면 얕은 판정이 캐시에 굳는다."""
    extractor = TitledTranscriptExtractor(Inner(error=TranscriptFetchError("IpBlocked")), Titles(TITLE))

    with pytest.raises(TranscriptFetchError):
        await extractor.fetch("vid")


async def test_제목이_짧아도_주장을_뽑을_수_있다():
    from healthcheck.pipeline.step3_claims import RuleBasedClaimExtractor

    content = await TitledTranscriptExtractor(Inner(NONE), Titles("냉동 블루베리로 당뇨 완치")).fetch("vid")
    result = RuleBasedClaimExtractor().extract(content.text)

    assert result.disease_cure_expression is True


# ── oEmbed 조회 ──────────────────────────────────────────────


def fetcher_with(handler) -> YouTubeTitleFetcher:
    return YouTubeTitleFetcher(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_oEmbed에서_제목을_읽는다():
    def handler(request):
        assert request.url.host == "www.youtube.com"
        assert request.url.path == "/oembed"
        return httpx.Response(200, json={"title": TITLE, "author_name": "채널"})

    assert await fetcher_with(handler).title("dQw4w9WgXcQ") == TITLE


@pytest.mark.parametrize("response", [httpx.Response(404), httpx.Response(401), httpx.Response(200, json={})])
async def test_제목을_못_구하면_None이다(response):
    """비공개·삭제 영상은 oEmbed가 401이나 404를 돌려준다."""
    assert await fetcher_with(lambda request: response).title("vid") is None


async def test_제목_조회_통신_오류도_예외를_올리지_않는다():
    def handler(request):
        raise httpx.ConnectError("연결 실패")

    assert await fetcher_with(handler).title("vid") is None
