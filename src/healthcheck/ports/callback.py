"""카카오 콜백 전송 인터페이스.

확인된 실제 스펙:
  - 콜백 URL은 스킬 요청 본문의 userRequest.callbackUrl에 담겨 온다
  - 즉시 응답은 template 없이 {"version": "2.0", "useCallback": true}
  - callbackUrl 유효 시간 5분, 1회만 사용 가능

유효 시간이 5분이고 1회 한정이라는 점이 워커 설계를 제약한다.
재시도는 5분 안에서만 의미가 있고, 한 번 성공하면 다시 보낼 수 없다.
"""

from __future__ import annotations

from typing import Protocol


class CallbackSender(Protocol):
    """최종 응답을 카카오가 알려준 콜백 URL로 POST한다."""

    async def send(self, callback_url: str, payload: dict) -> None: ...
