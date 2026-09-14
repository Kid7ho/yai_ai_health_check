"""네이버 블로그 스마트에디터 본문 추출.

실채널에서 공유된 네이버 블로그 글이 본문 1,707자인데 제목과 페이지 메타데이터
JSON만 추출됐다. 스마트에디터는 한 줄마다 짧은 문단 요소를 쓰고, trafilatura가
이 조각을 본문이 아닌 블록으로 보고 버렸다.
"""

from __future__ import annotations

import httpx

from healthcheck.adapters.web.article import (
    TrafilaturaArticleExtractor,
    _naver_smart_editor_text,
)

LINE = "홍삼을 꾸준히 섭취하면 면역력 증진에 도움을 줄 수 있다고 합니다"


def smart_editor_page(lines: int = 12) -> bytes:
    paragraphs = "".join(
        f'<p class="se-text-paragraph"><span>{LINE} {i}번째 줄</span></p>' for i in range(lines)
    )
    return f"""<html><head><meta charset="utf-8"><title>홍삼 이야기</title>
<script>{{"title":"홍삼 이야기","blogName":"예시"}}</script></head><body>
<div class="se-main-container">
  <div class="se-component se-text"><div class="se-module se-module-text">{paragraphs}</div></div>
  <div class="se-component se-image"><div class="se-module se-module-image"><img src="a.jpg"></div></div>
  <div class="se-component se-text"><div class="se-module se-module-text">
    <p class="se-text-paragraph"><span>\u200b</span></p>
    <p class="se-text-paragraph"><span>마지막 문단입니다</span></p></div></div>
</div>
<div class="comment">댓글: 마늘이 최고예요</div>
</body></html>""".encode("utf-8")


def test_글_모듈마다_한_줄로_이어_붙인다():
    text = _naver_smart_editor_text(smart_editor_page(lines=3))

    lines = text.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith(f"{LINE} 0번째 줄 {LINE} 1번째 줄")
    assert lines[1] == "마지막 문단입니다"
    assert "\u200b" not in text
    assert "댓글" not in text
    assert "blogName" not in text


def test_스마트에디터가_아니면_빈_문자열이다():
    assert _naver_smart_editor_text(b"<html><body><p>hello</p></body></html>") == ""


async def test_네이버_블로그는_스마트에디터_본문을_쓴다():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=smart_editor_page(), headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    extractor = TrafilaturaArticleExtractor(client=client, check_url=False)
    try:
        content = await extractor.fetch("https://blog.naver.com/example/223725247637")
    finally:
        await client.aclose()

    assert requested == ["https://m.blog.naver.com/example/223725247637"]
    assert f"{LINE} 11번째 줄" in content.text
    assert "마지막 문단입니다" in content.text
    assert "blogName" not in content.text
    assert content.text.startswith("홍삼 이야기")
