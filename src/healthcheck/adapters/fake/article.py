"""기사 본문 추출 Fake."""

from __future__ import annotations

import hashlib

from healthcheck.domain.types import ExtractedContent

FAKE_ARTICLES: dict[str, str] = {
    "https://news.example.com/ginseng": (
        "홍삼은 면역력 증진에 도움을 줄 수 있다고 알려져 있다. "
        "식품의약품안전처가 인정한 기능성 원료이기도 하다."
    ),
    "https://blog.example.com/blueberry": (
        "냉동 블루베리를 매일 먹으면 당뇨를 완치시킨다는 이야기가 돌고 있다."
    ),
}


class FakeArticleExtractor:
    def __init__(self, articles: dict[str, str] | None = None) -> None:
        self._articles = FAKE_ARTICLES if articles is None else articles
        self.calls: list[str] = []

    async def fetch(self, url: str) -> ExtractedContent:
        self.calls.append(url)
        text = self._articles.get(url)
        if not text:
            return ExtractedContent(text="", has_transcript=False, confidence=0.0)
        return ExtractedContent(
            text=text,
            has_transcript=True,
            confidence=0.85,
            language="ko",
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:32],
        )
