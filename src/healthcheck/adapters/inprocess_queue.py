"""인프로세스 asyncio 작업 큐.

Redis나 Celery는 도입하지 않는다. 단일 프로세스 안에서 asyncio 큐로 처리한다.
JobQueue 인터페이스를 만족하므로 나중에 교체할 수 있다.

큐 길이에 상한을 둔다. 상한이 없으면 요청이 몰릴 때 메모리가 계속 늘고,
콜백 URL 유효 시간 5분이 지난 작업까지 붙들고 있게 된다.
"""

from __future__ import annotations

import asyncio

from healthcheck.ports.queue import CheckJob


class QueueFull(RuntimeError):
    """큐가 꽉 찼다. 어르신에게는 잠시 뒤 다시 보내 달라고 답해야 한다."""


class InProcessJobQueue:
    def __init__(self, maxsize: int = 100) -> None:
        self._queue: asyncio.Queue[CheckJob] = asyncio.Queue(maxsize=maxsize)

    async def enqueue(self, job: CheckJob) -> None:
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull as error:
            raise QueueFull("처리 대기 중인 요청이 너무 많습니다.") from error

    async def dequeue(self) -> CheckJob:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    @property
    def size(self) -> int:
        return self._queue.qsize()

    async def join(self) -> None:
        """대기 중인 작업이 모두 끝날 때까지 기다린다. 테스트와 종료 처리에 쓴다."""
        await self._queue.join()
