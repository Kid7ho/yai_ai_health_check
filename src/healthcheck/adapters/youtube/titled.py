"""유튜브 영상 제목을 본문 앞에 붙인다.

점검에서 자막만 보고 영상 제목을 쓰지 않는 것을 확인했다. 건강 오정보는 핵심
주장이 제목에 담기는 경우가 많다. "홍삼 먹으면 당뇨 완치?" 같은 제목을 버리면
가장 위험한 문장을 놓친다.

제목은 oEmbed로 가져온다. 키가 필요 없고, 자막 서버와 다른 경로라 자막이
막혀도 대체로 된다. 실측에서 제목과 채널 이름이 바로 나왔다.

순서가 중요하다. 자막과 음성 인식을 먼저 시도하고, 둘 다 없을 때만 제목만으로
본문을 만든다. 이 추출기를 가장 바깥에 둬야 음성 인식이 제목에 가려지지 않는다.
"""

from __future__ import annotations

import hashlib
import logging
from urllib.parse import quote

import httpx

from healthcheck.domain.types import ExtractedContent
from healthcheck.ports.transcript import TranscriptExtractor

logger = logging.getLogger(__name__)

OEMBED_URL = "https://www.youtube.com/oembed?format=json&url="

# 제목만으로 본문을 만들 때의 신뢰도. 자막_신뢰도_최저(0.6)와 같다.
# 제목은 작성자가 직접 쓴 문장이라 무엇을 주장하는지는 정확하지만, 한 줄뿐이라
# 근거가 얇다. 판정은 하되 자막보다 앞서지 않게 둔다.
TITLE_ONLY_CONFIDENCE = 0.6


class YouTubeTitleFetcher:
    """oEmbed로 영상 제목을 가져온다. 실패하면 None을 돌려주고 예외를 올리지 않는다.

    제목은 있으면 좋은 정보일 뿐이다. 제목 조회 실패 때문에 자막 판정까지 막히면
    안 된다.
    """

    def __init__(self, *, timeout: float = 5.0, client: httpx.AsyncClient | None = None) -> None:
        self._timeout = timeout
        self._client = client
        self.call_count = 0

    async def title(self, video_id: str) -> str | None:
        if self._client is None:
            self._client = httpx.AsyncClient()
        self.call_count += 1

        url = OEMBED_URL + quote(f"https://www.youtube.com/watch?v={video_id}", safe="")
        try:
            response = await self._client.get(url, timeout=self._timeout)
            if response.status_code != 200:
                return None
            title = str(response.json().get("title") or "").strip()
            return title or None
        except Exception as error:
            logger.info("영상 제목 조회 실패 video=%s: %s", video_id, error)
            return None


class TitledTranscriptExtractor:
    def __init__(self, inner: TranscriptExtractor, titles: YouTubeTitleFetcher) -> None:
        self._inner = inner
        self._titles = titles

    async def fetch(self, video_id: str) -> ExtractedContent:
        # 자막 서버 차단 같은 일시적 실패는 그대로 올린다. 제목만으로 답해 버리면
        # 얕은 판정이 캐시에 굳고, 차단이 풀려도 자막으로 다시 판정하지 않는다.
        content = await self._inner.fetch(video_id)
        title = await self._titles.title(video_id)

        if not title:
            return content

        if content.has_transcript:
            text = f"{title}\n{content.text}"
            return ExtractedContent(
                text=text,
                has_transcript=True,
                confidence=content.confidence,
                language=content.language,
                content_hash=_digest(text),
                source=content.source,
            )

        logger.info("자막과 음성 인식이 없어 제목만으로 판정한다 video=%s", video_id)
        return ExtractedContent(
            text=title,
            has_transcript=True,
            confidence=TITLE_ONLY_CONFIDENCE,
            language="ko",
            content_hash=_digest(title),
            source="title_only",
        )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
