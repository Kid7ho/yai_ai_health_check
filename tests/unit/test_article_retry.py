"""기사 조회 일시 실패를 캐시하지 않는지 확인한다.

점검에서 사이트가 503을 돌려주면 '알 수 없어요'가 캐시에 저장되어 계속 같은
답을 주는 것을 확인했다. 유튜브 자막 차단은 막았는데 기사 쪽은 빠져 있었다.
"""

from __future__ import annotations

import pytest

from healthcheck.adapters.web.article import ArticleFetchError
from healthcheck.api.app import build_fake_orchestrator
from healthcheck.pipeline import step1_input
from healthcheck.pipeline.step6_response import render

URL = "https://news.example.com/article/1"


class FailingArticle:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def fetch(self, url: str):
        self.calls += 1
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        ArticleFetchError("기사 조회 응답 503", retryable=True),
        ArticleFetchError("기사 조회 응답 429", retryable=True),
        ArticleFetchError("통신 오류: timeout", retryable=True),
    ],
)
async def test_일시적_실패는_캐시하지_않는다(error):
    orchestrator = build_fake_orchestrator()
    orchestrator.article = FailingArticle(error)
    share = step1_input.classify(URL)

    result = await orchestrator.run(share)

    assert result.retryable_failure is True
    assert await orchestrator.cached(share) is None


async def test_일시적_실패는_다시_보내_달라고_안내한다():
    orchestrator = build_fake_orchestrator()
    orchestrator.article = FailingArticle(ArticleFetchError("기사 조회 응답 503", retryable=True))

    result = await orchestrator.run(step1_input.classify(URL))
    text = render(result)["template"]["outputs"][0]["simpleText"]["text"]

    assert "잠시 뒤에 다시" in text


@pytest.mark.parametrize(
    "error",
    [
        ArticleFetchError("기사 조회 응답 404", retryable=False),
        ArticleFetchError("본문을 읽을 수 없는 형식입니다: application/pdf"),
    ],
)
async def test_다시_해도_같은_실패는_캐시한다(error):
    """없는 글을 매번 다시 조회할 이유가 없다."""
    orchestrator = build_fake_orchestrator()
    orchestrator.article = FailingArticle(error)
    share = step1_input.classify(URL)

    await orchestrator.run(share)

    assert await orchestrator.cached(share) is not None


async def test_사이트가_살아나면_다음_요청에서_제대로_판정한다():
    from healthcheck.adapters.fake.article import FakeArticleExtractor

    orchestrator = build_fake_orchestrator()
    orchestrator.article = FailingArticle(ArticleFetchError("기사 조회 응답 503", retryable=True))
    share = step1_input.classify("https://news.example.com/ginseng")
    await orchestrator.run(share)

    orchestrator.article = FakeArticleExtractor()
    result = await orchestrator.run(share)

    assert result.retryable_failure is False
    assert result.content.has_transcript is True
