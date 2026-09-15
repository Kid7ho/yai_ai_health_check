"""② 내용 추출 품질 테스트.

점검에서 찾은 결함을 그대로 테스트로 남긴다.
  · 헤드라인이 빠졌다
  · 헤더에 문자셋이 없는 EUC-KR 페이지가 깨졌다
  · 응답 크기 상한이 다운로드 뒤에 적용됐다
  · 자막 길이 상한이 없었다
  · 로그인이 필요한 곳을 '알 수 없어요'로만 답했다
  · 종료 안내문 같은 짧은 페이지가 본문으로 통과했다
"""

from __future__ import annotations

import httpx
import pytest

from healthcheck.adapters.web.article import TrafilaturaArticleExtractor
from healthcheck.adapters.web.safe_http import safe_request
from healthcheck.api.app import build_fake_orchestrator
from healthcheck.domain.types import ExtractedContent
from healthcheck.pipeline import step1_input
from healthcheck.pipeline.orchestrator import MAX_CONTENT_CHARS
from healthcheck.pipeline.step6_response import DISCLAIMER, find_forbidden, render

BODY_SENTENCE = "홍삼은 면역력 증진에 도움을 줄 수 있다고 알려져 있습니다. "


def page(title: str, h1: str, paragraphs: int = 8, charset_meta: str = "utf-8") -> str:
    return (
        f"<html><head><meta charset='{charset_meta}'><title>{title}</title>"
        f"<meta property='og:title' content='{h1}'></head><body><article>"
        f"<h1>{h1}</h1><p>{BODY_SENTENCE * paragraphs}</p></article></body></html>"
    )


def extractor_for(content: bytes, content_type: str = "text/html") -> TrafilaturaArticleExtractor:
    def handler(request):
        return httpx.Response(200, content=content, headers={"content-type": content_type})

    return TrafilaturaArticleExtractor(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), check_url=False
    )


# ── 헤드라인 ─────────────────────────────────────────────────


async def test_헤드라인을_본문_앞에_붙인다():
    """건강 오정보는 핵심 주장이 제목에 담기는 경우가 많다."""
    html = page("사이트 이름", "냉동 블루베리가 당뇨를 완치시킨다").encode()
    content = await extractor_for(html).fetch("https://news.example.com/a")

    assert "완치시킨다" in content.text.split("\n")[0]
    assert "면역력" in content.text


async def test_헤드라인이_주장_추출까지_이어진다():
    from healthcheck.pipeline.step3_claims import RuleBasedClaimExtractor

    html = page("건강 뉴스", "냉동 블루베리가 당뇨를 완치시킨다").encode()
    content = await extractor_for(html).fetch("https://news.example.com/a")

    assert RuleBasedClaimExtractor().extract(content.text).disease_cure_expression is True


# ── 인코딩 ───────────────────────────────────────────────────


async def test_헤더에_문자셋이_없는_EUC_KR_페이지를_읽는다():
    """오래된 한국 사이트와 지역 언론에 흔한 형태다. 이전에는 한글이 깨졌다."""
    html = page("홍삼", "홍삼의 효능", charset_meta="euc-kr").encode("euc-kr")
    content = await extractor_for(html, "text/html").fetch("https://old.example.co.kr/a")

    assert "면역력" in content.text
    assert "�" not in content.text


# ── 크기 제한 ────────────────────────────────────────────────


async def test_받는_도중에_크기_상한에서_멈춘다():
    """이전에는 응답 전체를 받은 다음에 잘랐다."""
    sent = {"bytes": 0}

    async def stream():
        for _ in range(1000):
            chunk = b"a" * 10_000
            sent["bytes"] += len(chunk)
            yield chunk

    def handler(request):
        return httpx.Response(200, content=stream(), headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    response, _ = await safe_request(
        client, "GET", "https://big.example.com/", timeout=5, check_url=False, max_bytes=50_000
    )

    assert len(response.content) == 50_000
    # 1000개 조각(10MB)을 다 받지 않고 멈췄다.
    assert sent["bytes"] < 200_000


# ── 본문 길이 상한 ───────────────────────────────────────────


async def test_아주_긴_자막은_앞부분만_쓴다():
    """긴 방송 영상 자막이 26만 자까지 나왔다."""

    class Huge:
        async def fetch(self, video_id):
            return ExtractedContent(text="홍삼이 면역력에 좋다. " * 20000, has_transcript=True, confidence=0.9)

    orchestrator = build_fake_orchestrator()
    orchestrator.transcript = Huge()
    result = await orchestrator.run(step1_input.classify("https://youtu.be/redginseng1"))

    assert len(result.content.text) == MAX_CONTENT_CHARS


# ── 로그인이 필요한 곳 ───────────────────────────────────────


@pytest.mark.parametrize(
    "url,site",
    [
        ("https://cafe.naver.com/healthclub/12345", "네이버 카페"),
        ("https://www.instagram.com/p/C0000000000/", "인스타그램"),
        ("https://m.facebook.com/story.php?id=1", "페이스북"),
        ("https://vt.tiktok.com/ZSabc/", "틱톡"),
    ],
)
async def test_로그인이_필요한_곳은_조회하지_않고_이유를_안내한다(url, site):
    orchestrator = build_fake_orchestrator()
    result = await orchestrator.run(step1_input.classify(url))

    assert result.notice == f"login_required:{site}"
    assert result.verdict.rule_id == "INPUT-LOGIN_REQUIRED"
    assert orchestrator.article.calls == []

    texts = [o["simpleText"]["text"] for o in render(result)["template"]["outputs"] if "simpleText" in o]
    assert site in texts[0]
    assert "복사해서 보내" in texts[0]
    assert any(DISCLAIMER in t for t in texts)
    assert all(find_forbidden(text) == [] for text in texts)


async def test_일반_블로그는_로그인_안내를_하지_않는다():
    orchestrator = build_fake_orchestrator()
    result = await orchestrator.run(step1_input.classify("https://blog.example.com/blueberry"))
    assert result.notice is None


# ── 안내·오류 페이지 ─────────────────────────────────────────


@pytest.mark.parametrize(
    "notice",
    [
        "안녕하세요. 포스트 운영팀입니다. 네이버 포스트는 2025년 4월 서비스가 종료되었습니다. " * 2,
        "요청하신 페이지를 찾을 수 없습니다. 주소가 잘못되었거나 삭제되었거나 이동되었습니다. " * 3,
    ],
)
async def test_짧은_안내_오류_페이지는_본문으로_보지_않는다(notice):
    html = f"<html><body><article><p>{notice}</p></article></body></html>".encode()
    content = await extractor_for(html).fetch("https://site.example.com/a")

    assert content.has_transcript is False


async def test_너무_짧은_페이지는_본문으로_보지_않는다():
    html = "<html><body><article><p>홍삼 판매합니다. 문의 주세요.</p></article></body></html>".encode()
    assert (await extractor_for(html).fetch("https://shop.example.com/")).has_transcript is False


async def test_서비스_종료를_언급하는_긴_기사는_거르지_않는다():
    """안내문 판별은 짧은 페이지에만 쓴다. 긴 기사까지 거르면 안 된다."""
    text = BODY_SENTENCE * 25 + "한편 이 업체는 기존 서비스를 종료하고 새 제품을 냈다."
    html = f"<html><body><article><h1>홍삼 시장 동향</h1><p>{text}</p></article></body></html>".encode()

    assert (await extractor_for(html).fetch("https://news.example.com/a")).has_transcript is True


async def test_본문_출처를_기록한다():
    html = page("건강", "홍삼의 효능").encode()
    content = await extractor_for(html).fetch("https://news.example.com/a")
    assert content.source == "article"
