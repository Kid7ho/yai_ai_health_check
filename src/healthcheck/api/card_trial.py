"""결과 카드 모양 — 리스트 카드(기본)와 버튼 카드.

카카오 카드 버튼은 크기를 바꿀 수 없다. 버튼 대신 리스트 카드의 높은 줄로 보여 주는 모양을 시험판으로
휴대폰에서 비교했고, 리스트 모양이 더 낫다고 보아 기본으로 바꿨다(2026-10-01 사용자 결정).
채널에 "큰버튼끄기"를 보내면 그 사용자만 이전 버튼 카드로 받고, "큰버튼켜기"로 리스트 모양에 돌아온다.
비교용이라 메모리에만 두고 재시작하면 모두 리스트 모양이 된다.
"""

from __future__ import annotations

ON_COMMAND = "큰버튼켜기"
OFF_COMMAND = "큰버튼끄기"


class CardLayouts:
    MAX_USERS = 1000

    def __init__(self) -> None:
        # 버튼 카드로 되돌린 사용자. 나머지는 모두 리스트 모양이다.
        self._button_users: set[str] = set()

    def layout_for(self, user_key: str | None) -> str:
        return "buttons" if user_key and user_key in self._button_users else "list"

    def set(self, user_key: str | None, enabled: bool) -> None:
        """enabled면 리스트 모양(기본), 아니면 버튼 카드."""
        if not user_key:
            return
        if enabled:
            self._button_users.discard(user_key)
        elif len(self._button_users) < self.MAX_USERS:
            self._button_users.add(user_key)


def command_of(utterance: str) -> bool | None:
    """켜기면 True, 끄기면 False, 명령이 아니면 None. 띄어쓰기는 보지 않는다."""
    key = "".join(utterance.split())
    if key == ON_COMMAND:
        return True
    if key == OFF_COMMAND:
        return False
    return None
