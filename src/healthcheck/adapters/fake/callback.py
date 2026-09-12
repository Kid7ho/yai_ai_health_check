"""카카오 콜백 전송 Fake.

아직 실제 챗봇이 없으므로 전송 내용을 기록만 한다.
테스트는 이 기록을 보고 무엇이 어르신에게 전달됐는지 확인한다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SentCallback:
    callback_url: str
    payload: dict


class RecordingCallbackSender:
    """전송하지 않고 기록한다."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.sent: list[SentCallback] = []
        # 특정 URL에서 전송 실패를 재현하고 싶을 때 쓴다.
        self._fail_on = fail_on

    async def send(self, callback_url: str, payload: dict) -> None:
        if self._fail_on is not None and self._fail_on in callback_url:
            raise RuntimeError(f"콜백 전송 실패를 재현합니다: {callback_url}")
        self.sent.append(SentCallback(callback_url=callback_url, payload=payload))

    @property
    def last(self) -> SentCallback | None:
        return self.sent[-1] if self.sent else None
