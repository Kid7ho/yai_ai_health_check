"""비동기 작업 큐 인터페이스.

카카오 스킬 응답 제한은 5초인데 실제 파이프라인은 10~20초가 걸린다.
그래서 즉시 ack를 돌려주고 콜백으로 결과를 보내는 구조가 기본 경로다.

구현은 인프로세스 asyncio 큐로 시작한다. Redis나 Celery는 도입하지 않는다.
다만 나중에 교체할 수 있도록 이 인터페이스 뒤에 둔다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from healthcheck.domain.types import ShareInput


@dataclass(frozen=True, slots=True)
class CheckJob:
    """워커가 처리할 작업 하나."""

    job_id: str
    share_input: ShareInput
    # 카카오가 알려준 콜백 URL. 유효 시간 5분, 1회 한정이다.
    callback_url: str
    # 카카오 사용자 키. 개인정보이므로 보존 기간 정책을 따라 만료시킨다.
    user_key: str | None = None
    # 작업을 받은 시각. 콜백 URL 유효 시간(5분)을 넘겼는지 판단한다.
    created_at: float = field(default_factory=time.monotonic)
    # 결과 카드 위에 붙일 안내 말풍선. [한번 해 보기]가 어떤 문장을 확인했는지 보여 줄 때 쓴다.
    intro: str | None = None
    # try면 [한번 해 보기]가 대신 보낸 예시 문장이다. 응답 기록에서 어르신이 직접 보낸 입력과 구분한다.
    via: str | None = None


class JobQueue(Protocol):
    """작업을 넣고 꺼낸다."""

    async def enqueue(self, job: CheckJob) -> None: ...

    async def dequeue(self) -> CheckJob: ...
