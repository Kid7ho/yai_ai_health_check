"""기사·블로그 본문 추출 실제 구현.

trafilatura로 본문만 골라낸다. 광고와 댓글, 관련기사 목록이 섞이면 엉뚱한
성분과 효능이 잡힌다.

안전 장치 셋을 넣었다.

1. 주소 검사
   사용자가 아무 주소나 보낼 수 있다. 사내망이나 로컬호스트로 향하는 요청을
   막는다. 서버가 자기 자신이나 내부 서비스를 긁는 통로가 되면 안 된다.

2. 크기 제한
   본문이 아무리 길어도 정해진 양까지만 읽는다. 큰 파일을 보내 메모리를
   말리는 요청을 막는다.

3. 가져온 내용은 데이터로만 다룬다
   페이지에 어떤 문장이 있어도 그것이 판정 규칙을 바꾸지 못한다. 규칙 엔진은
   성분과 효능 표현만 본다.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx
import trafilatura

from healthcheck.domain.types import ExtractedContent

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 8.0
# 본문으로 읽어 들일 최대 바이트. 한국어 기사 한 편은 보통 수십 KB다.
MAX_BYTES = 2_000_000
# 이보다 짧으면 본문 추출에 실패한 것으로 본다. 제목만 남은 경우다.
MIN_TEXT_LENGTH = 80

# 브라우저가 아닌 요청을 막는 사이트가 많다.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class ArticleFetchError(RuntimeError):
    """본문 조회 실패. ② 단계가 자막 없음과 같게 취급한다."""


class UnsafeUrlError(ArticleFetchError):
    """내부망이나 허용되지 않는 주소."""


def assert_safe_url(url: str) -> None:
    """사내망·로컬호스트로 향하는 요청을 막는다.

    사용자가 보낸 주소를 서버가 그대로 긁으므로, 검사 없이 두면 서버가
    내부 서비스를 대신 조회해 주는 통로가 된다.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"허용되지 않는 주소 방식입니다: {parsed.scheme}")

    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("주소에 호스트가 없습니다.")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as error:
        raise ArticleFetchError(f"주소를 찾을 수 없습니다: {host}") from error

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
        ):
            raise UnsafeUrlError(f"내부망 주소로는 요청하지 않습니다: {host}")


class TrafilaturaArticleExtractor:
    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = MAX_BYTES,
        client: httpx.AsyncClient | None = None,
        check_url: bool = True,
    ) -> None:
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._client = client
        self._owns_client = client is None
        self._check_url = check_url
        self.call_count = 0

    async def fetch(self, url: str) -> ExtractedContent:
        if self._check_url:
            assert_safe_url(url)

        self.call_count += 1
        html = await self._download(url)

        text = trafilatura.extract(
            html,
            include_comments=False,  # 댓글에는 엉뚱한 성분 이야기가 섞인다
            include_tables=False,
            favor_precision=True,  # 본문이 아닌 블록을 버리는 쪽을 택한다
        )
        text = (text or "").strip()

        if len(text) < MIN_TEXT_LENGTH:
            logger.info("본문 추출 실패 또는 너무 짧다 url=%s 길이=%d", url, len(text))
            return ExtractedContent(text="", has_transcript=False, confidence=0.0)

        logger.info("기사 본문 확보 url=%s 글자수=%d", url, len(text))
        return ExtractedContent(
            text=text,
            has_transcript=True,
            # 기사는 사람이 쓴 글이라 자동 생성 자막보다 신뢰도가 높다.
            # 다만 본문 추출이 광고를 섞을 수 있어 수동 자막과 같게 두지는 않는다.
            confidence=0.85,
            language="ko",
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:32],
        )

    async def _download(self, url: str) -> str:
        client = self._client or httpx.AsyncClient(follow_redirects=True)
        self._client = client

        try:
            response = await client.get(
                url, timeout=self._timeout, headers={"User-Agent": _USER_AGENT}
            )
        except httpx.HTTPError as error:
            raise ArticleFetchError(f"기사 조회 통신 오류: {error}") from error

        if response.status_code != 200:
            raise ArticleFetchError(f"기사 조회 응답 {response.status_code}")

        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            raise ArticleFetchError(f"본문을 읽을 수 없는 형식입니다: {content_type}")

        return response.content[: self._max_bytes].decode(
            response.encoding or "utf-8", errors="replace"
        )

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
