"""유튜브 자막 추출 단위 테스트. 네트워크를 타지 않는다."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from youtube_transcript_api import IpBlocked, NoTranscriptFound, TranscriptsDisabled

from healthcheck.adapters.youtube.transcript import (
    TranscriptFetchError,
    YouTubeTranscriptExtractor,
    _clean,
)


@dataclass
class Snippet:
    text: str


@dataclass
class FetchedStub:
    snippets: list[Snippet]


@dataclass
class TranscriptStub:
    language_code: str
    is_generated: bool
    text: str = "홍삼이 면역력에 도움이 된다"

    def fetch(self) -> FetchedStub:
        return FetchedStub([Snippet(part) for part in self.text.split("|")])


class ListingStub:
    def __init__(self, manual=None, generated=None):
        self._manual = manual
        self._generated = generated

    def find_manually_created_transcript(self, languages):
        if self._manual is None:
            raise NoTranscriptFound("vid", languages, [])
        return self._manual

    def find_generated_transcript(self, languages):
        if self._generated is None:
            raise NoTranscriptFound("vid", languages, [])
        return self._generated


class ApiStub:
    def __init__(self, listing=None, error=None):
        self._listing = listing
        self._error = error

    def list(self, video_id: str):
        if self._error is not None:
            raise self._error
        return self._listing


async def test_사람이_올린_자막을_먼저_쓴다():
    manual = TranscriptStub("ko", is_generated=False)
    generated = TranscriptStub("ko", is_generated=True)
    extractor = YouTubeTranscriptExtractor(api=ApiStub(ListingStub(manual, generated)))

    content = await extractor.fetch("vid")

    assert content.has_transcript is True
    assert content.confidence == 0.9


async def test_자동_생성_자막도_받는다():
    """AI 제작 영상은 사람이 올린 자막이 거의 없다. 버리면 핵심 타깃을 놓친다."""
    extractor = YouTubeTranscriptExtractor(
        api=ApiStub(ListingStub(generated=TranscriptStub("ko", is_generated=True)))
    )

    content = await extractor.fetch("vid")

    assert content.has_transcript is True
    # 자막_신뢰도_최저 0.6보다 높아야 판정까지 간다.
    assert content.confidence > 0.6


async def test_한국어_자막이_없으면_없는_것으로_본다():
    """③ 구조화가 한국어 사전으로 동작한다. 영어 자막은 성분을 못 뽑는다."""
    extractor = YouTubeTranscriptExtractor(api=ApiStub(ListingStub()))

    content = await extractor.fetch("vid")

    assert content.has_transcript is False
    assert content.text == ""


@pytest.mark.parametrize("error", [TranscriptsDisabled("vid"), NoTranscriptFound("vid", [], [])])
async def test_자막이_없는_영상은_오류가_아니다(error):
    extractor = YouTubeTranscriptExtractor(api=ApiStub(error=error))

    content = await extractor.fetch("vid")

    assert content.has_transcript is False


async def test_IP_차단은_예외로_구분한다():
    """자막이 없는 것과 접근이 막힌 것은 다르다. 막힌 건 다시 시도해야 한다."""
    extractor = YouTubeTranscriptExtractor(api=ApiStub(error=IpBlocked("vid")))

    with pytest.raises(TranscriptFetchError):
        await extractor.fetch("vid")


async def test_자막_조각을_이어_붙이고_해시를_남긴다():
    transcript = TranscriptStub("ko", is_generated=False, text="홍삼이|면역력에|좋다")
    extractor = YouTubeTranscriptExtractor(api=ApiStub(ListingStub(transcript)))

    content = await extractor.fetch("vid")

    assert content.text == "홍삼이 면역력에 좋다"
    assert content.content_hash and len(content.content_hash) == 32


async def test_본문이_비면_자막_없음으로_본다():
    transcript = TranscriptStub("ko", is_generated=True, text="[음악]|[박수]")
    extractor = YouTubeTranscriptExtractor(api=ApiStub(ListingStub(generated=transcript)))

    assert (await extractor.fetch("vid")).has_transcript is False


def test_소리_표시를_지우고_공백을_정리한다():
    assert _clean("[음악] 홍삼이  면역력에\n좋다 (박수)") == "홍삼이 면역력에 좋다"


# ─────────────────────────────────────────────────────────────
# IP 차단 대응
# ─────────────────────────────────────────────────────────────


async def test_차단되면_이후_호출은_네트워크를_타지_않는다():
    """차단 상태에서 계속 부르면 차단 기간만 길어진다."""
    api = ApiStub(error=IpBlocked("vid"))
    extractor = YouTubeTranscriptExtractor(api=api, min_interval=0, cooldown=600)

    with pytest.raises(TranscriptFetchError):
        await extractor.fetch("vid1")

    assert extractor.is_blocked is True

    with pytest.raises(TranscriptFetchError, match="차단 대기"):
        await extractor.fetch("vid2")

    # 두 번째 호출은 실제 조회를 하지 않았다.
    assert extractor.call_count == 1
    assert extractor.blocked_count == 1


async def test_대기_시간이_지나면_다시_시도한다():
    extractor = YouTubeTranscriptExtractor(
        api=ApiStub(ListingStub(TranscriptStub("ko", is_generated=False))),
        min_interval=0,
        cooldown=0,
    )
    extractor._breaker.trip()

    content = await extractor.fetch("vid")

    assert content.has_transcript is True


async def test_자막이_없는_것은_차단으로_취급하지_않는다():
    """없는 자막 때문에 회로를 끊으면 정상 영상까지 막힌다."""
    extractor = YouTubeTranscriptExtractor(
        api=ApiStub(error=TranscriptsDisabled("vid")), min_interval=0
    )

    await extractor.fetch("vid")

    assert extractor.is_blocked is False


async def test_호출_간격을_지킨다():
    import time as _time

    extractor = YouTubeTranscriptExtractor(
        api=ApiStub(ListingStub(TranscriptStub("ko", is_generated=False))),
        min_interval=0.05,
    )

    started = _time.monotonic()
    await extractor.fetch("a")
    await extractor.fetch("b")

    assert _time.monotonic() - started >= 0.05


def test_설정에서_프록시_구성을_만든다():
    from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig

    from healthcheck.adapters.youtube.transcript import build_proxy_config
    from healthcheck.config import Settings

    assert build_proxy_config(Settings()) is None
    assert isinstance(
        build_proxy_config(Settings(youtube_proxy_url="http://proxy:8080")),
        GenericProxyConfig,
    )
    assert isinstance(
        build_proxy_config(Settings(webshare_username="u", webshare_password="p")),
        WebshareProxyConfig,
    )
