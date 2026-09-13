"""단축 링크 해석 실제 구현.

리다이렉트를 따라가 최종 주소를 알아낸다. 사용자가 보낸 주소를 서버가
따라가므로 기사 추출과 같은 안전 요청(safe_http)을 쓴다. 단계마다 내부망
여부를 검사한다. 단축 링크는 리다이렉트가 본업이라 이 검사가 특히 중요하다.

HEAD로 먼저 시도한다. 본문을 받지 않아 빠르다. 일부 단축 서비스는 HEAD를
거부하므로 그때만 GET으로 다시 시도한다.
"""

from __future__ import annotations

import logging

import httpx

from healthcheck.adapters.web.safe_http import FetchError, safe_request

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 6.0


class LinkResolveError(FetchError):
    """단축 링크를 풀지 못했다."""


class HttpLinkResolver:
    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
        check_url: bool = True,
    ) -> None:
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._check_url = check_url
        self.call_count = 0

    async def resolve(self, url: str) -> str:
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owns_client = True

        self.call_count += 1

        try:
            response, final = await safe_request(
                self._client, "HEAD", url, timeout=self._timeout, check_url=self._check_url
            )
            # HEAD를 거부하는 단축 서비스가 있다. 그때만 GET으로 다시 푼다.
            if response.status_code in (400, 403, 404, 405, 501):
                response, final = await safe_request(
                    self._client, "GET", url, timeout=self._timeout, check_url=self._check_url
                )
        except FetchError as error:
            raise LinkResolveError(f"단축 링크를 풀지 못했습니다: {error}") from error

        if final == url:
            raise LinkResolveError("단축 링크가 다른 주소로 이어지지 않습니다.")

        logger.info("단축 링크 해석: %s -> %s", url, final)
        return final

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
