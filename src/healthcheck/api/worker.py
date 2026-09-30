"""비동기 워커.

카카오 스킬 응답 제한은 5초인데 실제 파이프라인은 10~20초가 걸린다.
그래서 스킬 엔드포인트는 즉시 ack만 돌려주고, 실제 확인은 이 워커가 한다.

콜백 URL은 유효 5분에 1회만 쓸 수 있다. 그래서 어떤 경우에도 콜백을 한 번은
보내야 한다. 파이프라인이 실패해도 아무것도 안 보내면 어르신 화면에는
"확인하고 있어요"가 영원히 남는다. 실패 시에는 UNKNOWN 응답을 보낸다.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from healthcheck.domain.labels import Label
from healthcheck.domain.types import CheckResult, Verdict
from healthcheck.pipeline.orchestrator import Orchestrator
from healthcheck.pipeline.step6_response import render
from healthcheck.ports.callback import CallbackSender
from healthcheck.ports.events import EventLog
from healthcheck.ports.queue import CheckJob, JobQueue
from healthcheck.ports.repository import CheckRepository
from healthcheck.api.share import with_share_link
from healthcheck.storage.events import new_event, outcome_of
from healthcheck.storage.record import build_record

logger = logging.getLogger(__name__)

# 파이프라인이 실패했을 때 쓰는 판정. 규칙표를 거치지 않았다는 것을
# 규칙 ID로 구분할 수 있게 한다. 판정 로그에서 걸러 내기 위해서다.
_ERROR_RULE_ID = "ERR-PIPELINE"

# 콜백 URL은 5분간 유효하다. 여유를 두고 4분 30초가 지나면 보내지 않는다.
# 만료된 주소로 보내면 거절만 받고, 어르신 화면에는 이미 아무 답도 가지 않는다.
CALLBACK_DEADLINE_SECONDS = 270


class Worker:
    """큐에서 작업을 꺼내 파이프라인을 돌리고 콜백을 보낸다."""

    def __init__(
        self,
        queue: JobQueue,
        orchestrator: Orchestrator,
        callback_sender: CallbackSender,
        repository: CheckRepository | None = None,
        last_results=None,
        image_base_url: str | None = None,
        events: EventLog | None = None,
        web_share: bool = False,
    ) -> None:
        self._image_base_url = image_base_url
        self._events = events
        self._web_share = web_share
        self._queue = queue
        self._orchestrator = orchestrator
        self._callback = callback_sender
        self._repository = repository
        self._last_results = last_results

    async def run_forever(self) -> None:
        """취소될 때까지 작업을 처리한다. 앱 수명주기가 이 태스크를 관리한다."""
        while True:
            job = await self._queue.dequeue()
            try:
                await self.process(job)
            finally:
                task_done = getattr(self._queue, "task_done", None)
                if task_done is not None:
                    task_done()

    async def process(self, job: CheckJob) -> None:
        """작업 한 건. 콜백은 성공·실패와 무관하게 한 번 보낸다."""
        try:
            result = await self._orchestrator.run(job.share_input)
        except Exception:
            logger.exception("파이프라인 실패: job=%s", job.job_id)
            result = _failure_result()

        # 콜백 전송 성공 여부와 무관하게 먼저 기록한다. 전송이 실패해도 판정은
        # 일어난 일이고, 누적 확인 건수와 오정보 유형 수가 목표 지표다.
        await self._record(job, result)
        if self._last_results is not None:
            # "왜 그런지 보기" 버튼에 답하려면 직전 결과가 필요하다.
            self._last_results.put(job.user_key, result)

        import time

        # 응답 기록의 id를 먼저 정해 결과 카드 사진 링크에 건다. 공유된 카드가 열리면 이 id로 이어진다.
        event_id = uuid.uuid4().hex
        elapsed = time.monotonic() - job.created_at
        if elapsed > CALLBACK_DEADLINE_SECONDS:
            logger.warning(
                "콜백 유효 시간이 지나 보내지 않는다 job=%s 경과=%.0f초", job.job_id, elapsed
            )
            await self._log_event(job, result, delivered=False, event_id=event_id)
            return

        delivered = True
        try:
            payload = render(result, image_base_url=self._image_base_url, intro=job.intro)
            payload = with_share_link(
                payload, self._image_base_url, event_id, web_share=self._web_share, user_key=job.user_key,
                source_url=job.share_input.url,
            )
            await self._callback.send(job.callback_url, payload)
        except Exception:
            # 콜백은 1회만 쓸 수 있어서 여기서 재시도해도 성공하지 않는 경우가 많다.
            # 판정 자체는 캐시에 남았으므로 어르신이 다시 보내면 즉시 답할 수 있다.
            logger.exception("콜백 전송 실패: job=%s", job.job_id)
            delivered = False
        await self._log_event(job, result, delivered=delivered, event_id=event_id)

    async def _log_event(self, job: CheckJob, result: CheckResult, *, delivered: bool, event_id: str) -> None:
        """응답 기록. 요청을 받은 때부터 결과를 보낸 때까지 걸린 시간을 함께 남긴다."""
        if self._events is None:
            return
        import time

        try:
            await self._events.record(
                new_event(
                    "check",
                    user_key=job.user_key,
                    outcome=outcome_of(result),
                    share_input=job.share_input,
                    via=job.via,
                    latency_ms=int((time.monotonic() - job.created_at) * 1000),
                    delivered=delivered,
                    event_id=event_id,
                )
            )
        except Exception:
            logger.exception("응답 기록 실패: job=%s", job.job_id)


    async def _record(self, job: CheckJob, result: CheckResult) -> None:
        if self._repository is None:
            return
        try:
            await self._repository.save(
                build_record(
                    share_input=job.share_input, result=result, user_key=job.user_key
                )
            )
        except Exception:
            # 저장 실패로 어르신에게 답을 못 보내는 일은 만들지 않는다.
            logger.exception("판정 기록 저장 실패: job=%s", job.job_id)


def _failure_result() -> CheckResult:
    """파이프라인 실패 시 보낼 결과. 보수적으로 UNKNOWN이다."""
    return CheckResult(
        verdict=Verdict(
            label=Label.UNKNOWN,
            rule_id=_ERROR_RULE_ID,
            rules_version="-",
            reasons=("확인하는 중에 문제가 생겼어요.", "잠시 뒤에 다시 보내 주세요."),
        )
    )


async def start_workers(
    queue: JobQueue,
    orchestrator: Orchestrator,
    callback_sender: CallbackSender,
    count: int = 2,
    repository: CheckRepository | None = None,
    last_results=None,
    image_base_url: str | None = None,
    events: EventLog | None = None,
    web_share: bool = False,
) -> list[asyncio.Task]:
    """워커 여러 개를 띄운다.

    개수를 늘리면 동시 처리량이 늘지만, 외부 API 호출도 함께 늘어난다.
    Liner 토큰 비용을 지원받는 구조이므로 기본값은 작게 둔다.
    """
    worker = Worker(
        queue, orchestrator, callback_sender, repository, last_results, image_base_url, events, web_share
    )
    return [asyncio.create_task(worker.run_forever()) for _ in range(count)]
