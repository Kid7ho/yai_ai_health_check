"""⑦ 카카오 전달 점검에서 찾은 결함 테스트.

"왜 그런지 보기" 버튼은 누르면 버튼 글자를 사용자 발화로 다시 보낸다. 서버가 이를
새 확인 요청으로 처리해 근거 화면 대신 '알 수 없어요' 카드를 돌려줬다.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from healthcheck.adapters.fake.callback import RecordingCallbackSender
from healthcheck.adapters.inprocess_queue import InProcessJobQueue
from healthcheck.api.app import build_fake_orchestrator, create_app
from healthcheck.api.skill import LastResults
from healthcheck.api.worker import CALLBACK_DEADLINE_SECONDS, Worker
from healthcheck.pipeline import step1_input
from healthcheck.pipeline.step6_response import DISCLAIMER, render, render_reason
from healthcheck.ports.queue import CheckJob

REASON = "왜 그런지 보기"
CALLBACK = "https://bot-api.kakao.com/callback/abc"


def request_body(utterance: str, user: str = "user-a", callback: str | None = CALLBACK) -> dict:
    body = {"userRequest": {"utterance": utterance, "user": {"id": user}}}
    if callback:
        body["userRequest"]["callbackUrl"] = callback
    return body


def texts(payload: dict) -> list[str]:
    outs = payload["template"]["outputs"]
    return [o.get("simpleText", {}).get("text") or o.get("textCard", {}).get("description", "") for o in outs]


async def seeded_result(text: str = "홍삼이 면역력 증진에 도움을 줄 수 있다"):
    return await build_fake_orchestrator().run(step1_input.classify(text))


# ── 근거 버튼 ────────────────────────────────────────────────


async def test_근거_버튼을_누르면_직전_결과의_근거를_돌려준다():
    result = await seeded_result()
    app = create_app(build_fake_orchestrator(), worker_count=0)
    app.state.last_results.put("user-a", result)

    with TestClient(app) as client:
        body = client.post("/skill", json=request_body(REASON)).json()

    assert texts(body) == texts(render_reason(result))
    assert any(DISCLAIMER in t for t in texts(body))
    assert app.state.queue.size == 0


async def test_직전_결과가_없으면_먼저_보내_달라고_안내한다():
    app = create_app(build_fake_orchestrator(), worker_count=0)
    with TestClient(app) as client:
        body = client.post("/skill", json=request_body(REASON)).json()

    assert "먼저 확인하고 싶은" in texts(body)[0]
    assert app.state.queue.size == 0


async def test_다른_사용자의_결과는_보여_주지_않는다():
    app = create_app(build_fake_orchestrator(), worker_count=0)
    app.state.last_results.put("user-a", await seeded_result())

    with TestClient(app) as client:
        body = client.post("/skill", json=request_body(REASON, user="user-b")).json()

    assert "먼저 확인하고 싶은" in texts(body)[0]


async def test_워커가_결과를_사용자별로_보관한다():
    store = LastResults()
    worker = Worker(InProcessJobQueue(), build_fake_orchestrator(), RecordingCallbackSender(), None, store)
    job = CheckJob(job_id="j", share_input=step1_input.classify("홍삼이 면역력에 도움을 줍니다"), callback_url=CALLBACK, user_key="user-a")

    await worker.process(job)

    assert store.get("user-a") is not None


async def test_캐시로_답한_경우에도_결과를_보관한다():
    orchestrator = build_fake_orchestrator()
    share = step1_input.classify("홍삼이 면역력 증진에 도움을 줄 수 있다")
    await orchestrator.run(share)
    app = create_app(orchestrator, worker_count=0)

    with TestClient(app) as client:
        client.post("/skill", json=request_body("홍삼이 면역력 증진에 도움을 줄 수 있다"))

    assert app.state.last_results.get("user-a") is not None


def test_보관한_결과는_시간이_지나면_버린다():
    store = LastResults()
    store.TTL_SECONDS = 0
    store.put("user-a", object())
    time.sleep(0.01)
    assert store.get("user-a") is None


# ── 콜백 ─────────────────────────────────────────────────────


async def test_콜백_유효_시간이_지난_작업은_보내지_않는다():
    sender = RecordingCallbackSender()
    worker = Worker(InProcessJobQueue(), build_fake_orchestrator(), sender)
    job = CheckJob(
        job_id="old",
        share_input=step1_input.classify("홍삼이 면역력에 도움을 줍니다"),
        callback_url=CALLBACK,
        created_at=time.monotonic() - CALLBACK_DEADLINE_SECONDS - 1,
    )

    await worker.process(job)

    assert sender.sent == []


def test_콜백_허용_호스트를_설정으로_바꾼다(monkeypatch):
    from healthcheck.api.app import build_callback_sender
    from healthcheck.config import load_settings

    monkeypatch.setenv("ADAPTER_MODE", "live")
    monkeypatch.setenv("CALLBACK_ALLOWED_SUFFIXES", "kakao.com, kakaocdn.net")
    sender = build_callback_sender(load_settings(env_file=None))

    assert sender._allowed == ("kakao.com", "kakaocdn.net")


# ── 카카오 응답 형식 ─────────────────────────────────────────


async def test_모든_응답의_말풍선은_3개_이하다():
    """카카오 템플릿 outputs는 최대 3개다."""
    from healthcheck.domain.labels import Label
    from healthcheck.domain.types import CheckResult, Verdict

    result = await seeded_result()
    notice = CheckResult(verdict=Verdict(label=Label.UNKNOWN, rule_id="INPUT-X", rules_version="v"), notice="youtube_not_video")
    retry = CheckResult(verdict=Verdict(label=Label.UNKNOWN, rule_id="R-999", rules_version="v"), retryable_failure=True)

    for payload in (render(result), render_reason(result), render(notice), render(retry)):
        assert 1 <= len(payload["template"]["outputs"]) <= 3


def image_body(user: str = "user-a") -> dict:
    # 실채널에서 사진을 보냈을 때 온 구조다. 주소와 서명은 가짜다.
    url = "https://talk.kakaocdn.net/dna/x/y/i_abc.jpg?credential=AAA&expires=1&signature=BBB"
    body = request_body(url, user=user)
    body["userRequest"]["params"] = {"surface": "Kakaotalk.plusfriend", "media": {"type": "image", "url": url}}
    body["flow"] = {"trigger": {"type": "IMAGE_UPLOAD"}}
    return body


def test_사진을_보내면_조회하지_않고_바로_안내한다():
    sender = RecordingCallbackSender()
    app = create_app(orchestrator=build_fake_orchestrator(), callback_sender=sender)
    with TestClient(app) as client:
        response = client.post("/skill", json=image_body())

    payload = response.json()
    assert payload.get("useCallback") is not True
    assert "사진은 아직 확인할 수 없어요" in texts(payload)[0]
    assert sender.sent == []


def test_트리거만_업로드여도_사진으로_본다():
    body = image_body()
    del body["userRequest"]["params"]["media"]
    app = create_app(orchestrator=build_fake_orchestrator(), callback_sender=RecordingCallbackSender())
    with TestClient(app) as client:
        payload = client.post("/skill", json=body).json()
    assert "사진은 아직 확인할 수 없어요" in texts(payload)[0]
