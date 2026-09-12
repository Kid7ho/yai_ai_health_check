"""자막을 먼저 쓰고, 없으면 음성 인식으로 넘어가는 추출기.

TranscriptExtractor 인터페이스를 그대로 만족하므로 파이프라인은 이 조합이
붙었는지 모른다. 자막이 있으면 음성 인식을 부르지 않는다. 자막이 1초면
끝나는 일을 분 단위 작업으로 대신할 이유가 없다.
"""

from __future__ import annotations

import logging

from healthcheck.domain.types import ExtractedContent
from healthcheck.ports.audio import AudioTranscriber
from healthcheck.ports.transcript import TranscriptExtractor

logger = logging.getLogger(__name__)


class CaptionsThenAsrExtractor:
    def __init__(self, captions: TranscriptExtractor, asr: AudioTranscriber) -> None:
        self._captions = captions
        self._asr = asr

    async def fetch(self, video_id: str) -> ExtractedContent:
        try:
            content = await self._captions.fetch(video_id)
        except Exception as error:
            # 자막 서버가 막혔더라도 음성 인식은 될 수 있다. 경로가 다르다.
            logger.info("자막 실패, 음성 인식으로 넘어간다 video=%s (%s)", video_id, error)
            content = ExtractedContent(text="", has_transcript=False, confidence=0.0)
            return await self._try_asr(video_id, reraise=error)

        if content.has_transcript:
            return content

        logger.info("자막이 없어 음성 인식을 시도한다 video=%s", video_id)
        return await self._try_asr(video_id)

    async def _try_asr(self, video_id: str, reraise: Exception | None = None) -> ExtractedContent:
        try:
            return await self._asr.transcribe(video_id)
        except Exception as error:
            logger.warning("음성 인식도 실패 video=%s: %s", video_id, error)
            # 자막 쪽 실패가 일시적(차단)이었다면 그 예외를 올려 준다.
            # 캐시에 넣지 않아야 하는 상태이기 때문이다.
            if reraise is not None:
                raise reraise
            return ExtractedContent(text="", has_transcript=False, confidence=0.0)
