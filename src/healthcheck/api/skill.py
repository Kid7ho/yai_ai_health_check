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
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request

from healthcheck.api.auth import SkillAuth

from healthcheck.adapters.inprocess_queue import QueueFull
from healthcheck.pipeline import step1_input
from healthcheck.pipeline.orchestrator import Orchestrator
from healthcheck.pipeline.step6_response import _REASON_BUTTON, ack_response, render, render_reason
from healthcheck.ports.queue import CheckJob, JobQueue
from healthcheck.ports.repository import CheckRepository
from healthcheck.storage.record import build_record

logger = logging.getLogger(__name__)

def build_router(path: str = "/skill") -> APIRouter:
    """스킬 라우터를 만든다.

    경로를 설정으로 받는 이유는, 오픈빌더가 임의 헤더를 지원하지 않을 때
    추측하기 어려운 경로로 대신 보호할 수 있게 하려는 것이다.
    """
    router = APIRouter()
    router.add_api_route(path, skill, methods=["POST"])
    return router

_BUSY_TEXT = "지금 확인 요청이 많아요.\n잠시 뒤에 다시 보내 주세요."
_NO_CALLBACK_TEXT = (
    "지금은 바로 확인해 드릴 수 없어요.\n잠시 뒤에 다시 보내 주세요."
)


class LastResults:
    """사용자별 직전 확인 결과. "왜 그런지 보기" 버튼에 답하기 위해 둔다.

    카카오 버튼은 누르면 버튼 글자를 사용자 발화로 다시 보낸다. 직전 결과를
    기억하지 않으면 그 글자를 새 확인 요청으로 처리해 '알 수 없어요'를 돌려준다.
    메모리에만 두고 한 시간 뒤 버린다. 사용자 키를 오래 들고 있지 않기 위해서다.
    """

    TTL_SECONDS = 3600
    MAX_USERS = 5000

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, object]] = {}

    def put(self, user_key: str | None, result) -> None:
        if not user_key:
            return
        import time

        if len(self._items) >= self.MAX_USERS:
            oldest = min(self._items, key=lambda key: self._items[key][0])
            self._items.pop(oldest, None)
        self._items[user_key] = (time.monotonic(), result)

    def get(self, user_key: str | None):
        if not user_key or user_key not in self._items:
            return None
        import time

        stored_at, result = self._items[user_key]
        if time.monotonic() - stored_at > self.TTL_SECONDS:
            self._items.pop(user_key, None)
            return None
        return result


@dataclass(slots=True)
class SkillServices:
    """엔드포인트가 쓰는 것들. 앱 상태에 담아 두고 테스트에서 교체한다."""

    orchestrator: Orchestrator
    queue: JobQueue
    repository: CheckRepository | None = None
    auth: SkillAuth = field(default_factory=SkillAuth)
    last_results: LastResults = field(default_factory=LastResults)


async def skill(request: Request) -> dict:
    """스킬 요청 하나를 받는다. 5초 안에 반드시 돌려준다."""
    services: SkillServices = request.app.state.services

    # 인증부터 확인한다. 토큰이 설정되어 있지 않으면 통과한다.
    services.auth.verify(request)

    body = await request.json()

    if getattr(request.app.state, "log_skill_request", False):
        import json as _json

        logger.info(
            "카카오 요청 본문(식별자 가림): %s",
            _json.dumps(mask_body(body), ensure_ascii=False)[:4000],
        )
        logger.info(
            "카카오 요청 헤더 이름들: %s", sorted(request.headers.keys())
        )

    utterance = _dig(body, "userRequest", "utterance") or ""
    callback_url = _dig(body, "userRequest", "callbackUrl")
    user_key = _dig(body, "userRequest", "user", "id")

    # "왜 그런지 보기" 버튼. 새 확인 요청이 아니라 직전 결과의 근거를 돌려준다.
    # 외부 호출 없이 바로 답하므로 5초 안에 끝난다.
    if utterance.strip() == _REASON_BUTTON:
        previous = services.last_results.get(user_key)
        if previous is None:
            return _simple("먼저 확인하고 싶은 영상이나 글을 보내 주세요.")
        if previous.notice or previous.retryable_failure:
            return render(previous)
        return render_reason(previous)

    share_input = step1_input.classify(utterance)

    # 예외 경로: 캐시가 맞으면 외부 호출 없이 바로 답한다.
    cached = await services.orchestrator.cached(share_input)
    if cached is not None:
        logger.info("동기 응답(캐시 적중) kind=%s", share_input.kind)
        services.last_results.put(user_key, cached)
        # 캐시로 답한 것도 확인 건수에 들어간다. 어르신이 확인한 사실은 같다.
        if services.repository is not None:
            try:
                await services.repository.save(
                    build_record(
                        share_input=share_input, result=cached, user_key=user_key
                    )
                )
            except Exception:
                logger.exception("판정 기록 저장 실패(캐시 응답)")
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


# 로그에 남기기 전에 가릴 필드 경로. 사용자 식별자는 남기지 않는다.
# 실채널 요청은 properties에 같은 식별자를 스네이크 표기로 한 번 더 담아 보낸다.
_MASKED_KEYS = (
    "id",
    "botUserKey",
    "plusfriendUserKey",
    "appUserId",
    "bot_user_key",
    "plusfriend_user_key",
    "app_user_id",
)


def mask_body(value: Any) -> Any:
    """요청 본문에서 사용자 식별자를 가린다.

    연동 초기에 실제 요청 구조를 확인해야 하지만, 식별자를 로그에 남길
    이유는 없다. 값의 길이만 알 수 있게 바꿔 둔다.
    """
    if isinstance(value, dict):
        return {
            key: ("***" if key in _MASKED_KEYS and isinstance(inner, str) else mask_body(inner))
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [mask_body(item) for item in value]
    return value


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
