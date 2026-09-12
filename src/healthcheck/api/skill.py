"""카카오 스킬 엔드포인트.

기본 경로는 즉시 ack 후 콜백이다. 동기 응답은 캐시 적중 시의 예외 경로로만
존재한다. 이 순서를 바꾸면 5초 제한을 넘긴다.

확인된 카카오 요청 필드:
  userRequest.utterance    사용자 발화
  userRequest.callbackUrl  콜백 URL (유효 5분, 1회)
  userRequest.user.id      botUserKey
  userRequest.user.properties.isFriend  채널을 친구 추가한 사용자인지
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request

from healthcheck.adapters.inprocess_queue import QueueFull
from healthcheck.pipeline import step1_input
from healthcheck.pipeline.orchestrator import Orchestrator
from healthcheck.pipeline.step6_response import ack_response, render
from healthcheck.ports.queue import CheckJob, JobQueue

logger = logging.getLogger(__name__)

router = APIRouter()

_BUSY_TEXT = "지금 확인 요청이 많아요.\n잠시 뒤에 다시 보내 주세요."
_NO_CALLBACK_TEXT = (
    "지금은 바로 확인해 드릴 수 없어요.\n잠시 뒤에 다시 보내 주세요."
)


@dataclass(slots=True)
class SkillServices:
    """엔드포인트가 쓰는 것들. 앱 상태에 담아 두고 테스트에서 교체한다."""

    orchestrator: Orchestrator
    queue: JobQueue


@router.post("/skill")
async def skill(request: Request) -> dict:
    """스킬 요청 하나를 받는다. 5초 안에 반드시 돌려준다."""
    services: SkillServices = request.app.state.services
    body = await request.json()

    utterance = _dig(body, "userRequest", "utterance") or ""
    callback_url = _dig(body, "userRequest", "callbackUrl")
    user_key = _dig(body, "userRequest", "user", "id")

    share_input = step1_input.classify(utterance)

    # 예외 경로: 캐시가 맞으면 외부 호출 없이 바로 답한다.
    cached = await services.orchestrator.cached(share_input)
    if cached is not None:
        logger.info("동기 응답(캐시 적중) kind=%s", share_input.kind)
        return render(cached)

    # 기본 경로: 콜백이 없으면 비동기 처리를 할 수 없다.
    # TODO(확인필요): 오픈빌더에서 콜백 사용 설정을 켜야 callbackUrl이 온다.
    # 실제 채널 승인 후 이 분기가 실제로 발생하는지 확인해야 한다.
    if not callback_url:
        logger.warning("callbackUrl이 없어 비동기 처리를 못 한다")
        return _simple(_NO_CALLBACK_TEXT)

    job = CheckJob(
        job_id=uuid.uuid4().hex,
        share_input=share_input,
        callback_url=callback_url,
        user_key=user_key,
    )

    try:
        await services.queue.enqueue(job)
    except QueueFull:
        logger.warning("큐가 꽉 차서 요청을 받지 못했다")
        return _simple(_BUSY_TEXT)

    logger.info("작업 접수 job=%s kind=%s", job.job_id, share_input.kind)
    return ack_response()


def _simple(text: str) -> dict:
    """안내 문구만 돌려주는 응답.

    판정 결과가 아니므로 면책 문구를 붙이지 않는다. 판정 결과를 담는 응답은
    전부 step6_response.render()를 지나가며 거기서 면책 문구가 붙는다.
    """
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}


def _dig(body: Any, *path: str) -> Any:
    """중첩 필드를 안전하게 꺼낸다.

    카카오 요청 본문의 구조가 바뀌거나 필드가 빠져도 500을 내지 않는다.
    스킬 서버가 5초 안에 응답하지 못하면 어르신 화면에는 아무 답도 안 뜬다.
    """
    current = body
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
