"""큰 버튼 시험판 — 결과 카드를 리스트 카드 모양으로 받을 사용자.

카카오 카드 버튼은 크기를 바꿀 수 없어서, 버튼 대신 리스트 카드의 높은 줄로 보여 주는 모양을
휴대폰에서 비교해 보려고 둔다(2026-10-01). 채널에 "큰버튼켜기"를 보내면 그 사용자만 리스트 모양으로
받고, "큰버튼끄기"를 보내면 원래 모양으로 돌아간다. 시험용이라 메모리에만 두고 재시작하면 모두 꺼진다.
"""

from __future__ import annotations

ON_COMMAND = "큰버튼켜기"
OFF_COMMAND = "큰버튼끄기"


class ListCardTrial:
    MAX_USERS = 1000

    def __init__(self) -> None:
        self._users: set[str] = set()

    def layout_for(self, user_key: str | None) -> str:
        return "list" if user_key and user_key in self._users else "buttons"

    def set(self, user_key: str | None, enabled: bool) -> None:
        if not user_key:
            return
        if enabled and len(self._users) < self.MAX_USERS:
            self._users.add(user_key)
        elif not enabled:
            self._users.discard(user_key)


def command_of(utterance: str) -> bool | None:
    """켜기면 True, 끄기면 False, 시험 명령이 아니면 None. 띄어쓰기는 보지 않는다."""
    key = "".join(utterance.split())
    if key == ON_COMMAND:
        return True
    if key == OFF_COMMAND:
        return False
    return None
