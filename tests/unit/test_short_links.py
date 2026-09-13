"""단축 링크 해석과 영상 아닌 유튜브 링크 안내 테스트."""

from __future__ import annotations

import socket

import httpx
import pytest

from healthcheck.adapters.fake.links import FakeLinkResolver
from healthcheck.adapters.web.links import HttpLinkResolver, LinkResolveError
from healthcheck.adapters.web.safe_http import UnsafeUrlError
from healthcheck.api.app import build_fake_orchestrator
from healthcheck.domain.labels import Label
from healthcheck.pipeline import step1_input
from healthcheck.pipeline.step6_response import DISCLAIMER, find_forbidden, render

PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def fake_dns(monkeypatch):
    def resolve(host, *args, **kwargs):
        import ipaddress

        try:
            ipaddress.ip_address(host)
            address = host
        except ValueError:
            address = PUBLIC_IP
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def resolver_with(handler) -> HttpLinkResolver:
    return HttpLinkResolver(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def texts_of(payload: dict) -> list[str]:
    return [o["simpleText"]["text"] for o in payload["template"]["outputs"] if "simpleText" in o]


# ── 실제 해석기 ──────────────────────────────────────────────


async def test_리다이렉트를_따라가_최종_주소를_얻는다(fake_dns):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "naver.me":
            return httpx.Response(301, headers={"location": "https://blog.naver.com/a/123"})
        return httpx.Response(200)

    assert await resolver_with(handler).resolve("https://naver.me/xyz") == "https://blog.naver.com/a/123"


async def test_HEAD를_거부하면_GET으로_다시_푼다(fake_dns):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(405)
        if request.url.host == "kko.to":
            return httpx.Response(302, headers={"location": "https://youtu.be/dQw4w9WgXcQ"})
        return httpx.Response(200)

    assert await resolver_with(handler).resolve("https://kko.to/abc") == "https://youtu.be/dQw4w9WgXcQ"


async def test_단축_링크가_내부망으로_이어지면_막는다(fake_dns):
    """단축 링크는 리다이렉트가 본업이라 내부망 우회 경로로 쓰기 쉽다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})

    with pytest.raises(LinkResolveError):
        await resolver_with(handler).resolve("https://bit.ly/evil")


async def test_이어지는_주소가_없으면_풀지_못한_것으로_본다(fake_dns):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    with pytest.raises(LinkResolveError):
        await resolver_with(handler).resolve("https://bit.ly/dead")


# ── 파이프라인 연결 ──────────────────────────────────────────


async def test_단축_링크가_블로그로_이어지면_기사로_판정한다():
    orchestrator = build_fake_orchestrator()
    result = await orchestrator.run(step1_input.classify("이거 봐 https://naver.me/ginseng1"))

    assert orchestrator.link_resolver.calls == ["https://naver.me/ginseng1"]
    assert result.source_urls == ("https://blog.naver.com/anarche69/224382558879",)


async def test_단축_링크가_유튜브_영상으로_이어지면_자막을_가져온다():
    """단축 링크를 풀지 않던 때는 영상인데도 기사로 처리했다."""
    orchestrator = build_fake_orchestrator()
    result = await orchestrator.run(step1_input.classify("https://kko.to/video01"))

    assert orchestrator.transcript.calls == ["redginseng1"]
    assert result.verdict.label is Label.TRUSTWORTHY


async def test_단축_링크가_채널로_이어지면_안내한다():
    orchestrator = build_fake_orchestrator()
    result = await orchestrator.run(step1_input.classify("https://bit.ly/channel1"))

    assert result.notice == "youtube_not_video"


async def test_풀지_못한_단축_링크는_오류_없이_판정_보류다():
    orchestrator = build_fake_orchestrator()
    result = await orchestrator.run(step1_input.classify("https://bit.ly/unknown"))

    assert result.verdict.label is Label.UNKNOWN
    assert result.notice is None


async def test_같은_단축_링크가_다시_오면_캐시로_답한다():
    """원래 키와 풀린 키 둘 다에 저장한다. 동기 경로는 원래 키로만 찾는다."""
    orchestrator = build_fake_orchestrator()
    share = step1_input.classify("https://kko.to/video01")

    await orchestrator.run(share)
    cached = await orchestrator.cached(step1_input.classify("https://kko.to/video01"))

    assert cached is not None
    assert len(orchestrator.link_resolver.calls) == 1


async def test_긴_주소로_먼저_확인된_글은_단축_링크로_와도_캐시로_답한다():
    orchestrator = build_fake_orchestrator()
    await orchestrator.run(step1_input.classify("https://www.youtube.com/watch?v=redginseng1"))
    transcript_calls = len(orchestrator.transcript.calls)

    result = await orchestrator.run(step1_input.classify("https://kko.to/video01"))

    assert result.from_cache is True
    assert len(orchestrator.transcript.calls) == transcript_calls


# ── 영상 아닌 유튜브 링크 ────────────────────────────────────


async def test_채널_링크는_조회하지_않고_안내한다():
    orchestrator = build_fake_orchestrator()
    result = await orchestrator.run(step1_input.classify("https://www.youtube.com/@healthchannel"))

    assert result.notice == "youtube_not_video"
    assert result.verdict.rule_id == "INPUT-YOUTUBE_NOT_VIDEO"
    assert orchestrator.transcript.calls == []
    assert orchestrator.article.calls == []


def test_안내_응답에도_면책_문구가_붙고_금지_표현이_없다():
    from healthcheck.domain.types import CheckResult, Verdict

    result = CheckResult(
        verdict=Verdict(label=Label.UNKNOWN, rule_id="INPUT-YOUTUBE_NOT_VIDEO", rules_version="v0.7"),
        notice="youtube_not_video",
    )
    texts = texts_of(render(result))

    assert "영상 하나의 링크" in texts[0]
    assert DISCLAIMER in texts
    assert all(find_forbidden(text) == [] for text in texts)


def test_Fake_해석기도_같은_인터페이스를_만족한다():
    assert hasattr(FakeLinkResolver(), "resolve")
