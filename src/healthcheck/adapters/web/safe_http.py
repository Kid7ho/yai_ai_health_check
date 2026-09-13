"""외부 주소를 안전하게 가져오는 도구.

사용자가 보낸 주소를 서버가 그대로 가져온다. 검사 없이 두면 서버가 내부
서비스나 클라우드 인증 정보 주소(169.254.169.254)를 대신 조회해 주는 통로가
된다.

처음 주소만 검사하면 부족하다. 공개 주소가 내부 주소로 리다이렉트하면 그대로
따라가기 때문이다. 실제로 그 경로가 뚫려 있었다. 그래서 리다이렉트를 자동으로
따라가지 않고, 한 단계씩 직접 따라가며 매번 주소를 검사한다.

남은 위험 하나. 검사할 때 조회한 IP와 실제 연결할 때 조회한 IP가 다를 수 있다
(DNS 리바인딩). 이것까지 막으려면 연결 IP를 고정해야 하고, 프록시나 방화벽
단에서 내부망 송신을 막는 편이 확실하다. 배포 시 점검 항목으로 남긴다.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

MAX_REDIRECTS = 5

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class FetchError(RuntimeError):
    """외부 주소 조회 실패.

    retryable은 다시 시도하면 결과가 달라질 수 있는지다. 통신 오류, 시간 초과,
    5xx, 429가 여기 해당한다. 이런 실패로 나온 판정은 캐시하지 않는다.
    404나 내부망 차단은 다시 시도해도 같으므로 retryable이 아니다.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class UnsafeUrlError(FetchError):
    """내부망이나 허용되지 않는 주소."""


def assert_safe_url(url: str) -> None:
    """사내망·로컬호스트·링크로컬·예약 주소로 향하는 요청을 막는다."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"허용되지 않는 주소 방식입니다: {parsed.scheme}")

    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("주소에 호스트가 없습니다.")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as error:
        raise FetchError(f"주소를 찾을 수 없습니다: {host}") from error

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise UnsafeUrlError(f"내부망 주소로는 요청하지 않습니다: {host}")


async def safe_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    timeout: float,
    check_url: bool = True,
    max_redirects: int = MAX_REDIRECTS,
    max_bytes: int | None = None,
) -> tuple[httpx.Response, str]:
    """리다이렉트를 한 단계씩 따라가며 매 단계 주소를 검사한다.

    (최종 응답, 최종 주소)를 돌려준다. 최종 주소는 단축 링크를 푼 결과로도 쓴다.

    max_bytes를 주면 본문을 받는 도중에 끊는다. 이전에는 응답 전체를 받은 뒤에
    잘라서, 큰 파일이 오면 메모리를 다 쓴 다음에야 잘랐다.
    """
    current = url
    for _ in range(max_redirects + 1):
        if check_url:
            assert_safe_url(current)

        request = client.build_request(
            method, current, timeout=timeout, headers={"User-Agent": USER_AGENT}
        )
        try:
            response = await client.send(request, stream=True, follow_redirects=False)
        except httpx.HTTPError as error:
            # 통신 오류와 시간 초과는 잠시 뒤 다시 시도하면 될 수 있다.
            raise FetchError(f"통신 오류: {error}", retryable=True) from error

        if response.is_redirect:
            await response.aclose()
            location = response.headers.get("location")
            if not location:
                raise FetchError("리다이렉트 응답에 이동할 주소가 없습니다.")
            current = urljoin(current, location)
            continue

        try:
            body = await _read_limited(response, max_bytes)
        except httpx.HTTPError as error:
            raise FetchError(f"본문 수신 오류: {error}", retryable=True) from error
        finally:
            await response.aclose()

        # 스트림을 닫았으므로 받아 둔 본문으로 응답을 다시 만든다.
        # content-encoding은 이미 풀린 본문이라 헤더에서 뺀다.
        headers = [(k, v) for k, v in response.headers.items() if k.lower() != "content-encoding"]
        return (
            httpx.Response(response.status_code, headers=headers, content=body, request=request),
            current,
        )

    raise FetchError(f"리다이렉트가 {max_redirects}번을 넘었습니다.")


async def _read_limited(response: httpx.Response, max_bytes: int | None) -> bytes:
    """본문을 받되 max_bytes에서 멈춘다."""
    chunks: list[bytes] = []
    received = 0
    async for chunk in response.aiter_bytes():
        if max_bytes is not None and received + len(chunk) > max_bytes:
            chunks.append(chunk[: max_bytes - received])
            break
        chunks.append(chunk)
        received += len(chunk)
    return b"".join(chunks)
