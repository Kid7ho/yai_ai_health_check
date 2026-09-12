"""유튜브 자막 추출 실제 구현.

youtube-transcript-api를 쓴다. 공개 자막 트랙을 가져오는 방식이고 키가 필요
없다. YouTube Data API의 자막 다운로드는 영상 소유자 인증이 필요해서 쓸 수 없다.

이번 MVP는 음성 인식(ASR)을 하지 않는다. 자막이 없으면 예외가 아니라
has_transcript=False를 돌려주고 규칙 R-010이 판정을 보류한다.

한국어 자막만 받는다. ③ 주장 구조화가 한국어 사전과 패턴으로 동작하므로
영어 자막을 넘겨도 성분을 하나도 뽑지 못한다. 다른 언어 자막은 없는 것으로
취급하는 편이 정직하다.

라이브러리가 동기 호출이라 스레드로 넘긴다. 이벤트 루프를 막으면 5초 SLA를
지키는 다른 요청까지 함께 멈춘다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re

from youtube_transcript_api import (
    IpBlocked,
    NoTranscriptFound,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApi,
)

from healthcheck.domain.types import ExtractedContent

logger = logging.getLogger(__name__)

# 한국어 자막 코드. 지역 변형까지 받는다.
KOREAN_LANGUAGES = ("ko", "ko-KR")

# 사람이 올린 자막과 자동 생성 자막의 신뢰도.
#
# 자동 생성 자막은 오인식이 많다. 그래도 0.6(자막_신뢰도_최저)보다 살짝 높게
# 둔다. 발표 자료가 문제로 제시한 생성형 AI 제작 영상은 사람이 올린 자막이
# 거의 없어서, 자동 생성을 버리면 핵심 타깃이 전부 '알 수 없어요'가 된다.
#
# TODO(검수필요): 이 값이 커버리지와 오판 위험의 균형점을 정한다.
# 실제 자막 표본으로 오인식률을 재고 자문의와 함께 정해야 한다.
MANUAL_CONFIDENCE = 0.9
GENERATED_CONFIDENCE = 0.65

# 자막에는 [음악], (박수) 같은 소리 표시가 섞인다. 주장이 아니므로 지운다.
_NOISE = re.compile(r"[\[\(（【][^\]\)）】]{0,20}[\]\)）】]")
_WHITESPACE = re.compile(r"\s+")


class TranscriptFetchError(RuntimeError):
    """자막 서버 접근 자체가 막힌 경우.

    "자막이 없다"와 구분한다. 자막이 없으면 판정 보류가 정답이지만,
    차단이나 일시 장애는 다시 시도해야 하는 상태다.
    """


class YouTubeTranscriptExtractor:
    def __init__(
        self,
        *,
        languages: tuple[str, ...] = KOREAN_LANGUAGES,
        manual_confidence: float = MANUAL_CONFIDENCE,
        generated_confidence: float = GENERATED_CONFIDENCE,
        api: object | None = None,
    ) -> None:
        self._languages = languages
        self._manual_confidence = manual_confidence
        self._generated_confidence = generated_confidence
        self._api = api or YouTubeTranscriptApi()
        self.call_count = 0

    async def fetch(self, video_id: str) -> ExtractedContent:
        self.call_count += 1
        return await asyncio.to_thread(self._fetch_blocking, video_id)

    def _fetch_blocking(self, video_id: str) -> ExtractedContent:
        try:
            listing = self._api.list(video_id)
        except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as error:
            logger.info("자막 없음 video=%s (%s)", video_id, type(error).__name__)
            return _empty()
        except (IpBlocked, RequestBlocked, PoTokenRequired) as error:
            raise TranscriptFetchError(f"자막 서버 접근 차단: {type(error).__name__}") from error
        except Exception as error:  # 라이브러리가 새 예외를 던지는 경우
            raise TranscriptFetchError(f"자막 조회 실패: {error}") from error

        transcript = self._pick(listing, video_id)
        if transcript is None:
            return _empty()

        try:
            fetched = transcript.fetch()
        except Exception as error:
            raise TranscriptFetchError(f"자막 본문 조회 실패: {error}") from error

        text = _clean(" ".join(snippet.text for snippet in fetched.snippets))
        if not text:
            logger.info("자막 트랙은 있으나 본문이 비었다 video=%s", video_id)
            return _empty()

        confidence = (
            self._generated_confidence if transcript.is_generated else self._manual_confidence
        )
        logger.info(
            "자막 확보 video=%s 언어=%s 자동생성=%s 글자수=%d",
            video_id,
            transcript.language_code,
            transcript.is_generated,
            len(text),
        )

        return ExtractedContent(
            text=text,
            has_transcript=True,
            confidence=confidence,
            language=transcript.language_code,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:32],
        )

    def _pick(self, listing, video_id: str):
        """사람이 올린 한국어 자막을 먼저 찾고, 없으면 자동 생성을 쓴다."""
        for finder in (
            listing.find_manually_created_transcript,
            listing.find_generated_transcript,
        ):
            try:
                return finder(list(self._languages))
            except NoTranscriptFound:
                continue

        logger.info("한국어 자막이 없다 video=%s", video_id)
        return None


def _empty() -> ExtractedContent:
    return ExtractedContent(text="", has_transcript=False, confidence=0.0)


def _clean(text: str) -> str:
    """소리 표시를 지우고 공백을 정리한다."""
    return _WHITESPACE.sub(" ", _NOISE.sub(" ", text)).strip()
