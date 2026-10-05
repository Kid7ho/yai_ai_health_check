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

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request

from healthcheck.api.auth import SkillAuth

from healthcheck.adapters.inprocess_queue import QueueFull
from healthcheck.pipeline import step1_input
from healthcheck.pipeline.orchestrator import Orchestrator
from healthcheck.pipeline.step6_response import (
    _REASON_BUTTON,
    _SHARE_BUTTON,
    ack_response,
    guide_command,
    render_caution,
    render,
    render_guide,
    render_media_notice,
    render_reason,
    render_share,
    render_small_talk,
    render_welcome,
    try_intro,
    try_sentence,
)
from healthcheck.pipeline.small_talk import classify_small_talk
from healthcheck.pipeline.step3_claims import RuleBasedClaimExtractor
from healthcheck.ports.events import EventLog
from healthcheck.ports.queue import CheckJob, JobQueue
from healthcheck.ports.repository import CheckRepository
from healthcheck.api import card_trial
from healthcheck.api.card_trial import CardLayouts
from healthcheck.api.share import with_share_link
from healthcheck.storage.events import new_event, outcome_of
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
# 오픈빌더 기본 블록 이름. 채널에 처음 들어오면 이 블록이 불린다.
_WELCOME_INTENT = "웰컴 블록"
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
    # 주소가 아닌 말을 가를 때 쓰는 규칙 기반 추출기. 규칙 파일을 한 번만 읽도록 공유한다.
    small_talk_extractor: RuleBasedClaimExtractor = field(default_factory=RuleBasedClaimExtractor)
    # 결과 카드 머리 이미지의 공개 주소. 없으면 이미지 없는 카드로 답한다.
    image_base_url: str | None = None
    # 응답 기록(교육 성과 KPI). 없으면 기록하지 않는다.
    events: EventLog | None = None
    # 카카오 공식 공유 기능을 쓰는가(KAKAO_JS_KEY). 쓰면 [친구에게 보내기]가 공유 페이지를 연다.
    web_share: bool = False
    # 결과 카드의 [원본 정보 확인하기] 주소를 서명하는 키(KAKAO_ADMIN_KEY). 없으면 원본으로 바로 간다.
    source_secret: str | None = None
    # 결과 카드 모양(기본 버튼 카드, "큰버튼켜기"를 보낸 사용자는 리스트 카드). 워커와 같은 객체를 쓴다.
    card_trial: CardLayouts = field(default_factory=CardLayouts)


async def _log(services: SkillServices, kind: str, user_key: str | None, **fields) -> None:
    """응답 한 줄을 기록한다. 기록 실패로 어르신에게 답을 못 보내는 일은 만들지 않는다."""
    if services.events is None:
        return
    try:
        await services.events.record(new_event(kind, user_key=user_key, **fields))
    except Exception:
        logger.exception("응답 기록 실패 kind=%s", kind)


async def skill(request: Request) -> dict:
    """스킬 요청 하나를 받는다. 5초 안에 반드시 돌려준다."""
    services: SkillServices = request.app.state.services
    started = time.monotonic()

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

    # 콜백 유효 시간 시험. 설정으로 켰을 때만 동작한다.
    if getattr(request.app.state, "callback_probe_enabled", False):
        match = _PROBE.match(utterance.strip())
        if match and callback_url:
            delay = min(int(match.group(1)), _PROBE_MAX_SECONDS)
            task = asyncio.create_task(
                _probe_callback(request.app.state.callback_sender, callback_url, delay)
            )
            _PROBE_TASKS.add(task)
            task.add_done_callback(_PROBE_TASKS.discard)
            logger.warning("콜백 유효 시간 시험 접수 delay=%d초", delay)
            return ack_response()

    # 사진. 실채널에서 사진을 보내면 utterance에 카카오 CDN 서명 주소가 오고
    # params.media.type이 image다. 링크로 처리하면 서버가 사진을 내려받고
    # '알 수 없어요'를 돌려준다. 이미지 판독은 범위 밖이므로 조회 없이 안내한다.
    if _is_media_upload(body):
        logger.info("사진·파일 입력이라 확인하지 않는다")
        await _log(services, "media", user_key, outcome="media")
        return render_media_notice()

    # 큰 버튼(리스트 카드) 켜기·끄기. 직전 결과가 있으면 바뀐 모양으로 다시 보여 줘서 바로 비교하게 한다.
    trial = card_trial.command_of(utterance)
    if trial is not None:
        services.card_trial.set(user_key, trial)
        logger.info("큰 버튼 %s", "켬" if trial else "끔")
        notice = "큰 버튼으로 보여 드릴게요." if trial else "작은 버튼으로 보여 드릴게요."
        previous = services.last_results.get(user_key)
        if previous is None:
            return _simple(f"{notice} 이제 영상이나 글을 보내 보세요.")
        payload = render(
            previous, image_base_url=services.image_base_url, intro=f"{notice} 직전 결과를 다시 보여 드려요.",
            layout=services.card_trial.layout_for(user_key),
        )
        return with_share_link(
            payload, services.image_base_url, uuid.uuid4().hex, web_share=services.web_share, user_key=user_key,
            source_url=_first_source(previous), source_secret=services.source_secret,
        )

    # 사용법·처음 인사. 외부 호출 없이 바로 답한다.
    # 오픈빌더 웰컴 블록을 이 스킬에 연결하면 채널에 처음 들어온 어르신에게 처음 인사가 나간다.
    # TODO(확인필요): 웰컴 블록 요청의 intent.name이 "웰컴 블록"인지 실채널에서 확인한다.
    command = guide_command(utterance)
    if command:
        # 채팅방 메뉴·버튼이 제대로 오는지 로그로 확인할 수 있게 남긴다. 말 자체는 남기지 않는다.
        logger.info("안내 응답 kind=%s", command)
    if _dig(body, "intent", "name") == _WELCOME_INTENT or command == "welcome":
        logger.info("안내 응답 kind=welcome intent=%s", _dig(body, "intent", "name"))
        # 채널 진입으로 불린 인사는 어르신이 보낸 메시지가 아니다. KPI의 단계 수에서 뺀다.
        await _log(services, "welcome", user_key, outcome="command" if command == "welcome" else "intent")
        return render_welcome()
    if command == "guide":
        await _log(services, "guide", user_key)
        return render_guide(services.image_base_url)
    if command == "caution":
        await _log(services, "caution", user_key)
        return render_caution()
    intro = None
    via = None
    if command == "try":
        await _log(services, "try", user_key)
        via = "try"
        # [한번 해 보기]는 예시 문장을 대신 확인한다. 한 번 확인한 뒤로는 캐시로 바로 답한다.
        # 결과 위에 확인한 문장을 보여 줘서 어르신이 무엇을 보내면 되는지 배우게 한다.
        utterance = try_sentence()
        intro = try_intro()

    # "왜 그런지 보기" 버튼. 새 확인 요청이 아니라 직전 결과의 근거를 돌려준다.
    # 외부 호출 없이 바로 답하므로 5초 안에 끝난다.
    if utterance.strip() == _REASON_BUTTON:
        previous = services.last_results.get(user_key)
        await _log(services, "reason", user_key, outcome=outcome_of(previous) if previous else "none")
        if previous is None:
            return _simple("먼저 확인하고 싶은 영상이나 글을 보내 주세요.")
        if previous.notice or previous.retryable_failure:
            return render(previous, image_base_url=services.image_base_url)
        return render_reason(previous, image_base_url=services.image_base_url)

    # "친구에게 보내기" 버튼. 누른 수를 세고, 공유 버튼 달린 같은 카드를 다시 보낸다.
    if utterance.strip() == _SHARE_BUTTON:
        previous = services.last_results.get(user_key)
        await _log(services, "share", user_key, outcome=outcome_of(previous) if previous else "none")
        if previous is None:
            return _simple("먼저 확인하고 싶은 영상이나 글을 보내 주세요.")
        return render_share(previous, image_base_url=services.image_base_url)

    share_input = step1_input.classify(utterance)

    # 주소가 아닌 말. 인사·감사·질문·건강과 무관한 말은 확인 과정(약 15초)에 넣지 않고 바로 답한다.
    if share_input.kind == "PLAIN_TEXT":
        if not share_input.raw_text.strip():
            await _log(services, "welcome", user_key, outcome="empty")
            return render_welcome()
        small_talk = classify_small_talk(share_input.raw_text, services.small_talk_extractor)
        if small_talk is not None:
            logger.info("바로 답하는 말 kind=%s", small_talk)
            await _log(services, "small_talk", user_key, outcome=small_talk)
            return render_small_talk(small_talk)

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
        event_id = uuid.uuid4().hex
        await _log(
            services, "check", user_key, outcome=outcome_of(cached), share_input=share_input, via=via,
            latency_ms=int((time.monotonic() - started) * 1000), delivered=True, event_id=event_id,
        )
        payload = render(
            cached, image_base_url=services.image_base_url, intro=intro, layout=services.card_trial.layout_for(user_key)
        )
        return with_share_link(
            payload, services.image_base_url, event_id, web_share=services.web_share, user_key=user_key,
            source_url=share_input.url, source_secret=services.source_secret,
        )

    # 기본 경로: 콜백이 없으면 비동기 처리를 할 수 없다.
    # TODO(확인필요): 오픈빌더에서 콜백 사용 설정을 켜야 callbackUrl이 온다.
    # 실제 채널 승인 후 이 분기가 실제로 발생하는지 확인해야 한다.
    if not callback_url:
        logger.warning("callbackUrl이 없어 비동기 처리를 못 한다")
        await _log(services, "check", user_key, outcome="no_callback", share_input=share_input, via=via, delivered=False)
        return _simple(_NO_CALLBACK_TEXT)

    job = CheckJob(
        job_id=uuid.uuid4().hex,
        share_input=share_input,
        callback_url=callback_url,
        user_key=user_key,
        intro=intro,
        via=via,
    )

    try:
        await services.queue.enqueue(job)
    except QueueFull:
        logger.warning("큐가 꽉 차서 요청을 받지 못했다")
        await _log(services, "check", user_key, outcome="busy", share_input=share_input, via=via, delivered=True)
        return _simple(_BUSY_TEXT)

    logger.info("작업 접수 job=%s kind=%s", job.job_id, share_input.kind)
    return ack_response()


def _first_source(result) -> str | None:
    return result.source_urls[0] if result.source_urls else None


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


_PROBE = re.compile(r"^콜백시험\s+(\d{1,3})$")
_PROBE_MAX_SECONDS = 290
_PROBE_TASKS: set[asyncio.Task] = set()


async def _probe_callback(sender, callback_url: str, delay: int) -> None:
    """delay초 뒤 콜백을 보내고 받아졌는지 기록한다. 판정 결과가 아니므로 카드를 만들지 않는다."""
    await asyncio.sleep(delay)
    payload = {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": f"콜백 시험: {delay}초 뒤에 보낸 메시지입니다."}}]},
    }
    try:
        await sender.send(callback_url, payload)
        logger.warning("콜백 유효 시간 시험 결과 delay=%d초 성공", delay)
    except Exception as error:
        logger.warning("콜백 유효 시간 시험 결과 delay=%d초 실패: %s", delay, error)


def _is_media_upload(body: Any) -> bool:
    """사진·파일 업로드 요청인가.

    실채널에서 확인한 두 표시 중 하나라도 있으면 업로드로 본다.
    params.media.type과 flow.trigger.type(IMAGE_UPLOAD)이다.
    """
    media = _dig(body, "userRequest", "params", "media")
    if isinstance(media, dict) and media.get("type"):
        return True
    trigger = _dig(body, "flow", "trigger", "type")
    return isinstance(trigger, str) and trigger.endswith("_UPLOAD")


# 서명이 붙은 주소. 카카오 CDN 사진 주소는 쿼리에 접근 서명을 담는다.
# 로그에 남으면 기록을 본 사람이 사진을 열 수 있으므로 쿼리를 통째로 가린다.
_SIGNED_QUERY = re.compile(r"\?[^\s\"]*(?:credential|signature|token|expires)=[^\s\"]*", re.IGNORECASE)


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
    if isinstance(value, str):
        return _SIGNED_QUERY.sub("?(가림)", value)
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
