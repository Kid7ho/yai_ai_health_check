"""카카오 콜백 전송기 테스트. 네트워크를 타지 않는다."""

from __future__ import annotations

import httpx
import pytest

from healthcheck.adapters.kakao.callback import (
    CallbackError,
    CallbackUrlRejected,
    HttpCallbackSender,
)

PAYLOAD = {"version": "2.0", "template": {"outputs": []}}
URL = "https://bot-api.kakao.com/callback/abc123"


def sender_with(handler, **kwargs) -> HttpCallbackSender:
    return HttpCallbackSender(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        check_host=False,
        **kwargs,
    )


async def test_콜백을_POST로_보낸다():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok"})

    sender = sender_with(handler)
    await sender.send(URL, PAYLOAD)

    assert seen["method"] == "POST"
    assert seen["body"] == PAYLOAD
    assert sender.sent_count == 1


async def test_4xx는_재시도하지_않는다():
    """콜백은 1회만 쓸 수 있다. 이미 쓴 주소를 다시 불러도 같은 결과다."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="already used")

    with pytest.raises(CallbackError, match="400"):
        await sender_with(handler).send(URL, PAYLOAD)

    assert len(calls) == 1


async def test_5xx는_한_번_재시도한다():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200)

    sender = sender_with(handler)
    await sender.send(URL, PAYLOAD)

    assert len(calls) == 2
    assert sender.sent_count == 1


async def test_통신_오류가_계속되면_예외를_올린다():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결 실패")

    with pytest.raises(CallbackError):
        await sender_with(handler).send(URL, PAYLOAD)


# ── 콜백 주소 검증 ───────────────────────────────────────────
#
# 콜백 URL은 외부에서 들어온 값이다. 검증 없이 POST하면 판정 결과와 사용자
# 발화를 요청자가 지정한 임의의 서버로 보내는 통로가 된다.


def real_sender() -> HttpCallbackSender:
    return HttpCallbackSender()


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/steal",
        "https://kakao.com.evil.example.com/steal",
        "http://bot-api.kakao.com/callback/abc",
        "https://127.0.0.1/callback",
    ],
)
async def test_허용되지_않은_콜백_주소로는_보내지_않는다(url):
    with pytest.raises(CallbackUrlRejected):
        await real_sender().send(url, PAYLOAD)


async def test_카카오_호스트는_허용한다():
    """실제 전송은 하지 않고 주소 검증만 확인한다."""
    sender = HttpCallbackSender()
    sender._assert_allowed("https://bot-api.kakao.com/callback/abc")
    sender._assert_allowed("https://kakao.com/callback/abc")


async def test_허용_목록을_설정으로_바꿀_수_있다():
    """실제 콜백 호스트를 확인하면 목록만 고치면 된다."""
    sender = HttpCallbackSender(allowed_suffixes=("example.com",))
    sender._assert_allowed("https://example.com/abc")

    with pytest.raises(CallbackUrlRejected):
        sender._assert_allowed("https://bot-api.kakao.com/abc")
