"""응답 기록 — 어르신이 무엇을 보내고 어떤 답을 받았는지 한 줄씩 남긴다.

판정 기록(CheckRecord)은 신호등이 나온 확인만 담는다. 교육 성과를 재려면 사용법 버튼,
인사말, 사진, "왜 그런지 보기", "친구에게 보내기"처럼 판정이 아닌 응답도 알아야 한다.
참여자 수, 첫 확인까지 걸린 단계, 막힌 뒤 이탈, 재사용률이 모두 이 기록에서 나온다.

보낸 글 내용은 담지 않는다. 누가(익명 번호) 언제 어떤 종류의 답을 받았는지만 남긴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

# 응답 종류
#   check       확인 요청(링크·문장)에 대한 답. outcome에 신호등·안내·실패가 담긴다
#   guide       사용법   caution 주의사항   try 한번 해 보기 버튼
#   welcome     처음 인사(채널 진입 또는 "처음")
#   small_talk  인사·감사 같은 짧은 말
#   media       사진·파일
#   reason      "왜 그런지 보기"   share "친구에게 보내기"(시험 기간의 메시지 버튼)
#   share_open  공유된 결과 카드의 사진을 누르고 들어온 것. 누른 사람은 모르므로 user_hash가 없다
#   poster_scan 포스터 QR을 찍고 들어온 것. outcome에 장소 태그가 담긴다
#   source      결과 카드의 [원본 정보 확인하기]. ref가 결과(check)의 event_id다
EventKind = Literal[
    "check", "guide", "caution", "try", "welcome", "small_talk", "media", "reason", "share", "share_open",
    "share_sent", "poster_scan", "source",
]


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    created_at: datetime
    # 카카오 사용자 키를 해시한 익명 번호. 판정 기록의 보존 기간 뒤 해시와 같은 방식이다.
    user_hash: str | None
    kind: str
    # check: 신호등(TRUSTWORTHY 등) · out_of_scope · notice:종류 · retry · error · busy · no_callback
    # reason·share: 직전 결과의 신호등 또는 none   small_talk: 인사 종류   welcome: intent 또는 command
    outcome: str | None = None
    # 확인 요청의 입력 종류와 형태. 글 내용이 아니라 분류만 담는다.
    input_kind: str | None = None
    input_form: str | None = None
    site: str | None = None
    # try면 [한번 해 보기]가 대신 보낸 예시 문장이다. 어르신이 직접 보낸 입력과 구분한다.
    via: str | None = None
    # 요청을 받은 뒤 답이 도착하기까지(밀리초). 콜백이면 전송 완료 시각까지다.
    latency_ms: int | None = None
    # 답을 실제로 보냈는가. 콜백 전송 실패나 유효 시간 초과면 False다.
    delivered: bool | None = None
    # share_open: 어느 결과 카드(check 기록의 event_id)가 공유되어 열렸는가
    ref: str | None = None


class EventLog(Protocol):
    async def record(self, event: Event) -> None: ...

    async def list_events(self) -> list[Event]: ...

    async def purge_expired(self, now: datetime | None = None) -> int:
        """보존 기간이 지난 기록을 지운다. 지운 건수를 돌려준다."""
        ...
