"""② 내용 추출 — 유튜브 자막 추출기 인터페이스.

이번 MVP는 자막이 있는 영상만 지원한다. 음성 인식(ASR)은 명시적으로 범위 밖이다.
자막이 없으면 예외를 던지지 말고 has_transcript=False인 결과를 돌려준다.
판정 보류(UNKNOWN)는 정상 동작이지 오류가 아니기 때문이다.
"""

from __future__ import annotations

from typing import Protocol

from healthcheck.domain.types import ExtractedContent


class TranscriptExtractor(Protocol):
    """유튜브 영상 ID로 자막을 가져온다."""

    async def fetch(self, video_id: str) -> ExtractedContent: ...
