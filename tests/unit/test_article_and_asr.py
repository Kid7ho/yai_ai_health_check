"""기사 본문 추출과 음성 인식 대체 경로 테스트. 네트워크를 타지 않는다."""

from __future__ import annotations

import httpx
import pytest

from healthcheck.adapters.fake.article import FakeArticleExtractor
from healthcheck.adapters.web.article import (
    ArticleFetchError,
    TrafilaturaArticleExtractor,
    UnsafeUrlError,
    assert_safe_url,
)
from healthcheck.adapters.youtube.asr import AsrError
from healthcheck.adapters.youtube.fallback import CaptionsThenAsrExtractor
from healthcheck.adapters.youtube.transcript import TranscriptFetchError
from healthcheck.domain.types import ExtractedContent

ARTICLE_HTML = """
<html><head><title>홍삼의 효능</title></head><body>
<nav>메뉴 광고 배너</nav>
<article><h1>홍삼의 효능</h1>
<p>홍삼은 면역력 증진에 도움을 줄 수 있다고 알려져 있습니다. 식품의약품안전처가
인정한 기능성 원료이며, 여러 연구에서 면역 지표 개선이 보고되었습니다.</p>
<p>다만 질병을 치료한다는 광고는 법으로 금지되어 있습니다. 제품을 고를 때는
인정 문구를 확인하는 것이 좋습니다.</p></article>
<div class="comments"><p>저는 이걸로 당뇨를 완치했어요</p></div>
</body></html>
"""


def extractor_with(handler) -> TrafilaturaArticleExtractor:
    return TrafilaturaArticleExtractor(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), check_url=False
    )


# ── 기사 본문 추출 ───────────────────────────────────────────


async def test_기사_본문을_뽑는다():
    extractor = extractor_with(lambda request: httpx.Response(200, html=ARTICLE_HTML))

    content = await extractor.fetch("https://news.example.com/a")

    assert content.has_transcript is True
    assert "면역력 증진" in content.text
    assert content.confidence == 0.85
    assert content.content_hash


async def test_댓글은_본문에서_제외한다():
    """댓글에 섞인 완치 후기를 기사 주장으로 읽으면 판정이 뒤집힌다."""
    extractor = extractor_with(lambda request: httpx.Response(200, html=ARTICLE_HTML))

    content = await extractor.fetch("https://news.example.com/a")

    assert "당뇨를 완치했어요" not in content.text


async def test_본문이_너무_짧으면_없는_것으로_본다():
    extractor = extractor_with(
        lambda request: httpx.Response(200, html="<html><body><p>짧음</p></body></html>")
    )

    assert (await extractor.fetch("https://news.example.com/a")).has_transcript is False


async def test_자동_요청을_막는_사이트는_실패로_올린다():
    """실제로 한국경제 기사가 403을 돌려줬다."""
    extractor = extractor_with(lambda request: httpx.Response(403, text="forbidden"))

    with pytest.raises(ArticleFetchError, match="403"):
        await extractor.fetch("https://news.example.com/a")


async def test_HTML이_아니면_읽지_않는다():
    extractor = extractor_with(
        lambda request: httpx.Response(200, json={"a": 1}, headers={"content-type": "application/json"})
    )

    with pytest.raises(ArticleFetchError, match="형식"):
        await extractor.fetch("https://news.example.com/a.json")


# ── 주소 안전 검사 ───────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8000/health", "http://localhost/admin", "http://169.254.169.254/latest/meta-data"],
)
def test_내부망_주소는_거부한다(url):
    """사용자가 보낸 주소를 서버가 그대로 긁는다. 검사 없이 두면 내부 조회 통로가 된다."""
    with pytest.raises(UnsafeUrlError):
        assert_safe_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/a"])
def test_http가_아닌_주소는_거부한다(url):
    with pytest.raises(UnsafeUrlError):
        assert_safe_url(url)


def test_일반_주소는_통과한다():
    assert_safe_url("https://ko.wikipedia.org/wiki/홍삼")


# ── 자막 → 음성 인식 대체 경로 ───────────────────────────────

CAPTION = ExtractedContent(text="자막 본문", has_transcript=True, confidence=0.65)
NO_CAPTION = ExtractedContent(text="", has_transcript=False, confidence=0.0)
ASR_RESULT = ExtractedContent(text="음성 인식 본문", has_transcript=True, confidence=0.62)


class Stub:
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.calls = 0

    async def fetch(self, video_id):
        return await self._run()

    async def transcribe(self, video_id):
        return await self._run()

    async def _run(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.content


async def test_자막이_있으면_음성_인식을_부르지_않는다():
    """자막이 1초면 끝나는 일을 분 단위 작업으로 대신할 이유가 없다."""
    captions, asr = Stub(CAPTION), Stub(ASR_RESULT)

    content = await CaptionsThenAsrExtractor(captions, asr).fetch("vid")

    assert content.text == "자막 본문"
    assert asr.calls == 0


async def test_자막이_없으면_음성_인식으로_넘어간다():
    captions, asr = Stub(NO_CAPTION), Stub(ASR_RESULT)

    content = await CaptionsThenAsrExtractor(captions, asr).fetch("vid")

    assert content.text == "음성 인식 본문"
    assert content.confidence < 0.65  # 자막보다 낮게 둔다
    assert asr.calls == 1


async def test_자막_서버가_막혀도_음성_인식을_시도한다():
    """자막과 음성은 경로가 다르다. 실제로 자막이 차단된 상태에서 음성은 됐다."""
    captions = Stub(error=TranscriptFetchError("IpBlocked"))
    asr = Stub(ASR_RESULT)

    content = await CaptionsThenAsrExtractor(captions, asr).fetch("vid")

    assert content.text == "음성 인식 본문"


async def test_둘_다_실패하면_차단_예외를_그대로_올린다():
    """일시적 실패는 캐시하지 않아야 하므로 예외를 삼키지 않는다."""
    captions = Stub(error=TranscriptFetchError("IpBlocked"))
    asr = Stub(error=AsrError("내려받기 실패"))

    with pytest.raises(TranscriptFetchError):
        await CaptionsThenAsrExtractor(captions, asr).fetch("vid")


async def test_자막_없음과_음성_실패가_겹치면_판정_보류로_간다():
    captions = Stub(NO_CAPTION)
    asr = Stub(error=AsrError("음성 인식 실패"))

    content = await CaptionsThenAsrExtractor(captions, asr).fetch("vid")

    assert content.has_transcript is False


# ── 파이프라인 연결 ──────────────────────────────────────────


async def test_기사_링크가_판정까지_간다():
    from healthcheck.api.app import build_fake_orchestrator
    from healthcheck.pipeline import step1_input

    orchestrator = build_fake_orchestrator()
    share = step1_input.classify("https://blog.example.com/blueberry")

    result = await orchestrator.run(share)

    assert share.kind == "OTHER_LINK"
    assert result.verdict.rule_id in ("R-020", "R-021")
    assert result.claims.disease_cure_expression is True


async def test_본문을_못_읽은_기사는_판정_보류다():
    from healthcheck.api.app import build_fake_orchestrator
    from healthcheck.pipeline import step1_input

    orchestrator = build_fake_orchestrator()

    result = await orchestrator.run(step1_input.classify("https://news.example.com/없는기사"))

    assert result.verdict.rule_id == "R-010"


def test_기사_Fake도_같은_인터페이스를_만족한다():
    assert hasattr(FakeArticleExtractor(), "fetch")
