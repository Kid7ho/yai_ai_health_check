"""음성 인식 인터페이스.

지시서는 이번 MVP에서 음성 인식을 범위 밖으로 두었다. 그 뒤 자막이 없는
영상도 처리해 달라는 요청이 있어 추가했다. 기본값은 꺼짐이다.

자막이 있으면 자막을 쓴다. 음성 인식은 자막이 없을 때만 쓰는 대체 경로다.
자막보다 느리고 부정확하며, 영상 길이에 비례해 시간이 든다.
"""

from __future__ import annotations

from typing import Protocol

from healthcheck.domain.types import ExtractedContent


class AudioTranscriber(Protocol):
    """영상의 음성을 글로 옮긴다."""

    async def transcribe(self, video_id: str) -> ExtractedContent: ...
