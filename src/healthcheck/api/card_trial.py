"""결과 카드 모양 — 버튼 카드(기본)와 리스트 카드(큰 버튼).

카카오 카드 버튼은 크기를 바꿀 수 없어 리스트 카드의 높은 줄로 보여 주는 모양을 만들었다.
2026-10-01 리스트 모양을 기본으로 했다가 2026-10-05 사용자 요청으로 버튼 카드 기본으로 되돌렸다.
채널에 "큰버튼켜기"를 보내면 그 사용자만 리스트 모양으로 받고, "큰버튼끄기"로 버튼 카드에 돌아온다.
메모리에만 두고 재시작하면 모두 버튼 카드가 된다.
"""

from __future__ import annotations

ON_COMMAND = "큰버튼켜기"
OFF_COMMAND = "큰버튼끄기"


class CardLayouts:
    MAX_USERS = 1000

    def __init__(self) -> None:
        # 큰 버튼(리스트 모양)을 켠 사용자. 나머지는 모두 버튼 카드다.
        self._list_users: set[str] = set()

    def layout_for(self, user_key: str | None) -> str:
        return "list" if user_key and user_key in self._list_users else "buttons"

    def set(self, user_key: str | None, enabled: bool) -> None:
        """enabled면 리스트 모양, 아니면 버튼 카드(기본)."""
        if not user_key:
            return
        if not enabled:
            self._list_users.discard(user_key)
        elif len(self._list_users) < self.MAX_USERS:
            self._list_users.add(user_key)


def command_of(utterance: str) -> bool | None:
    """켜기면 True, 끄기면 False, 명령이 아니면 None. 띄어쓰기는 보지 않는다."""
    key = "".join(utterance.split())
    if key == ON_COMMAND:
        return True
    if key == OFF_COMMAND:
        return False
    return None
