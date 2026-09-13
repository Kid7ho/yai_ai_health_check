"""외부 주소 안전 요청 테스트.

리다이렉트를 따라가다 내부망으로 빠지는 경로를 막는지 확인한다. 이전 구현은
처음 주소만 검사해서 공개 주소가 169.254.169.254로 리다이렉트하면 그대로
따라갔다.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from healthcheck.adapters.web.article import TrafilaturaArticleExtractor
from healthcheck.adapters.web.safe_http import (
    FetchError,
    UnsafeUrlError,
    assert_safe_url,
    safe_request,
)

PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """네트워크 없이 돌도록 이름 조회를 고정한다. IP 문자열은 그대로 돌려준다."""

    def resolve(host, *args, **kwargs):
        try:
            import ipaddress

            ipaddress.ip_address(host)
            address = host
        except ValueError:
            address = {"localhost": "127.0.0.1", "internal.corp": "10.0.0.5"}.get(host, PUBLIC_IP)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_공개_주소가_내부망으로_리다이렉트하면_막는다():
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    with pytest.raises(UnsafeUrlError):
        await safe_request(client_with(handler), "GET", "https://public.example.com/a", timeout=5)

    # 내부 주소에는 요청이 한 번도 가지 않았다.
    assert hits == ["https://public.example.com/a"]


async def test_여러_단계_리다이렉트의_중간도_검사한다():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.example.com":
            return httpx.Response(301, headers={"location": "https://b.example.com/"})
        return httpx.Response(302, headers={"location": "http://internal.corp/admin"})

    with pytest.raises(UnsafeUrlError):
        await safe_request(client_with(handler), "GET", "https://a.example.com/", timeout=5)


async def test_상대_경로_리다이렉트를_따라간다():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/new"})
        return httpx.Response(200, text="ok")

    response, final = await safe_request(
        client_with(handler), "GET", "https://public.example.com/old", timeout=5
    )

    assert response.status_code == 200
    assert final == "https://public.example.com/new"


async def test_리다이렉트가_끝없이_이어지면_멈춘다():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://public.example.com/loop"})

    with pytest.raises(FetchError, match="넘었습니다"):
        await safe_request(client_with(handler), "GET", "https://public.example.com/loop", timeout=5)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://10.0.0.5/",
        "http://internal.corp/",
        "http://169.254.169.254/",
        "http://0.0.0.0/",
        "file:///etc/passwd",
    ],
)
def test_내부망과_허용되지_않는_방식을_거부한다(url):
    with pytest.raises(UnsafeUrlError):
        assert_safe_url(url)


async def test_기사_추출기도_리다이렉트_우회를_막는다():
    """실제로 뚫려 있던 경로다. 기사 추출기가 공용 안전 요청을 쓰는지 확인한다."""
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        if request.url.host == "public.example.com":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
        return httpx.Response(200, html="<p>" + "내부 비밀 " * 50 + "</p>")

    extractor = TrafilaturaArticleExtractor(client=client_with(handler))

    with pytest.raises(UnsafeUrlError):
        await extractor.fetch("https://public.example.com/a")

    assert not any("169.254" in hit for hit in hits)
