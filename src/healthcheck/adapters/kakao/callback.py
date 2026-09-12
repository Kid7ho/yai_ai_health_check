"""카카오 콜백 전송 실제 구현.

확인된 스펙:
  콜백 URL은 요청 본문의 userRequest.callbackUrl에 담겨 온다
  유효 5분, 1회만 사용 가능
  요청 포맷은 스킬 응답 포맷과 같다

설계에서 신경 쓴 것 셋.

1. 주소를 검증한다
   콜백 URL은 외부에서 들어온 값이다. 검증 없이 그대로 POST하면 판정 결과를
   임의의 서버로 보내는 통로가 된다. 사용자 발화와 판정 내용이 함께 나가므로
   개인정보 유출 경로이기도 하다. 허용 호스트 목록과 내부망 차단을 둔다.

2. 재시도는 짧게, 한 번만
   콜백은 5분 안에 1회만 쓸 수 있다. 성공하면 다시 보낼 수 없고, 오래 매달려도
   유효 시간을 넘기면 의미가 없다. 네트워크 오류와 5xx만 한 번 재시도한다.

3. 실패를 삼키지 않는다
   여기서 예외를 올리면 워커가 로그를 남긴다. 판정 자체는 캐시와 저장소에
   남아 있으므로 어르신이 다시 보내면 즉시 답할 수 있다.

TODO(확인필요): 실제 콜백 호스트를 확인하지 못했다. 기본 허용 목록은
kakao.com 계열로 두었고, 채널 승인 후 실제 주소를 보고 확정해야 한다.
목록에 없는 호스트는 보내지 않고 예외를 올린다.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
# 카카오 콜백 URL의 유효 시간이 5분이다. 그 안에 끝나야 한다.
DEFAULT_ALLOWED_SUFFIXES = ("kakao.com", "kakaoenterprise.com")


class CallbackError(RuntimeError):
    """콜백 전송 실패."""


class CallbackUrlRejected(CallbackError):
    """허용되지 않은 콜백 주소. 보내지 않았다."""


class HttpCallbackSender:
    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        allowed_suffixes: tuple[str, ...] = DEFAULT_ALLOWED_SUFFIXES,
        max_retries: int = 1,
        client: httpx.AsyncClient | None = None,
        check_host: bool = True,
    ) -> None:
        self._timeout = timeout
        self._allowed = allowed_suffixes
        self._max_retries = max_retries
        self._client = client
        self._owns_client = client is None
        self._check_host = check_host
        self.sent_count = 0

    async def send(self, callback_url: str, payload: dict) -> None:
        if self._check_host:
            self._assert_allowed(callback_url)

        client = self._ensure_client()
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await client.post(
                    callback_url,
                    json=payload,
                    timeout=self._timeout,
                    headers={"Content-Type": "application/json"},
                )
            except httpx.HTTPError as error:
                last_error = error
                logger.warning("콜백 통신 오류 (%d회차): %s", attempt + 1, error)
            else:
                if response.status_code < 300:
                    self.sent_count += 1
                    logger.info("콜백 전송 완료 status=%d", response.status_code)
                    return

                # 4xx는 다시 보내도 같은 결과다. 콜백이 이미 만료됐거나
                # 한 번 쓴 주소일 수 있다.
                if response.status_code < 500:
                    raise CallbackError(
                        f"콜백 전송 거부 {response.status_code}: {response.text[:150]}"
                    )

                last_error = CallbackError(f"콜백 응답 {response.status_code}")
                logger.warning("콜백 재시도 대상 응답 %d", response.status_code)

            if attempt < self._max_retries:
                await asyncio.sleep(0.5)

        raise CallbackError("콜백 전송 실패") from last_error

    def _assert_allowed(self, callback_url: str) -> None:
        """콜백 주소가 카카오 것인지 확인한다.

        판정 결과와 사용자 발화가 함께 나가므로, 주소를 검증하지 않으면
        요청을 보낸 쪽이 지정한 임의의 서버로 그 내용이 전달된다.
        """
        parsed = urlparse(callback_url)
        if parsed.scheme != "https":
            raise CallbackUrlRejected(f"https가 아닌 콜백 주소입니다: {parsed.scheme}")

        host = (parsed.hostname or "").lower()
        if not host:
            raise CallbackUrlRejected("콜백 주소에 호스트가 없습니다.")

        if not any(host == suffix or host.endswith(f".{suffix}") for suffix in self._allowed):
            raise CallbackUrlRejected(f"허용되지 않은 콜백 호스트입니다: {host}")

        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as error:
            raise CallbackUrlRejected(f"콜백 호스트를 찾을 수 없습니다: {host}") from error

        for info in infos:
            address = ipaddress.ip_address(info[4][0])
            if address.is_private or address.is_loopback or address.is_link_local:
                raise CallbackUrlRejected(f"내부망으로 향하는 콜백 주소입니다: {host}")

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
