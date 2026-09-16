"""외부 API 응답을 녹화해 두고 같은 응답으로 다시 측정한다.

골든셋이 실제 Liner 검색과 식약처 API를 부르면 점수가 외부 사정에 흔들린다.
실측에서 Liner 검색 결과가 바뀌어 gold-114가 틀렸고, 식약처 API의 09~19시
제한 때문에 4건이 틀렸다. 코드나 규칙표를 고친 효과만 보려면 입력이 고정돼야 한다.

HTTP 층에서 녹화하므로 관련성 판단·찬반 분류·규칙표를 고치면 그 효과가 그대로 드러난다.
녹화 파일에는 API 키를 남기지 않는다. 식약처 주소의 키 자리는 KEY로 바꾸고 헤더는 저장하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

import httpx

Mode = Literal["record", "replay"]

# 식품안전나라 주소는 경로에 인증키가 들어간다: /api/{키}/I-0040/...
_MFDS_KEY = re.compile(r"(/api/)[^/]+(/I-\d+/)")


class ReplayMissingError(RuntimeError):
    """녹화에 없는 요청. 검색어가 바뀌었으면 다시 녹화해야 한다."""


def request_key(request: httpx.Request) -> str:
    url = _MFDS_KEY.sub(r"\1KEY\2", str(request.url))
    body = request.content or b""
    digest = hashlib.sha256(body).hexdigest()[:16] if body else "-"
    return f"{request.method} {url} {digest}"


class RecordReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, path: Path | str, mode: Mode, *, reuse_recorded: bool = False) -> None:
        self._path = Path(path)
        self._mode = mode
        # 녹화할 때 이미 있는 응답은 다시 부르지 않는다. 후보를 조금씩 늘리는 분류 시트용이다.
        # 골든셋 재녹화는 외부 응답을 새로 받는 것이 목적이므로 기본은 끈다.
        self._reuse_recorded = reuse_recorded
        self._entries: dict[str, dict] = {}
        if self._path.is_file():
            self._entries = json.loads(self._path.read_text(encoding="utf-8"))
        elif mode == "replay":
            raise FileNotFoundError(f"녹화 파일이 없습니다: {self._path}")
        self._inner = httpx.AsyncHTTPTransport() if mode == "record" else None
        self.used: set[str] = set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = request_key(request)
        self.used.add(key)
        if self._mode == "replay":
            entry = self._entries.get(key)
            if entry is None:
                raise ReplayMissingError(f"녹화에 없는 요청입니다. 다시 녹화하세요: {key}")
            return httpx.Response(
                entry["status"],
                content=entry["body"].encode("utf-8"),
                headers={"content-type": entry.get("content_type", "application/json")},
                request=request,
            )

        if self._reuse_recorded and key in self._entries:
            entry = self._entries[key]
            return httpx.Response(
                entry["status"],
                content=entry["body"].encode("utf-8"),
                headers={"content-type": entry.get("content_type", "application/json")},
                request=request,
            )

        response = await self._inner.handle_async_request(request)
        body = await response.aread()
        self._entries[key] = {
            "status": response.status_code,
            "content_type": response.headers.get("content-type", "application/json"),
            "body": body.decode("utf-8", errors="replace"),
        }
        # aread()가 압축을 이미 풀었다. 원래 헤더의 content-encoding을 그대로 넘기면 호출 쪽이
        # 한 번 더 풀려다 실패해 녹화 중 조회가 전부 실패로 잡혔다.
        headers = {"content-type": response.headers.get("content-type", "application/json")}
        return httpx.Response(response.status_code, content=body, headers=headers, request=request)

    def save(self) -> None:
        """이번 측정에 쓰인 요청만 남긴다. 버린 검색어가 쌓이지 않게 한다."""
        kept = {key: self._entries[key] for key in sorted(self.used) if key in self._entries}
        self._path.write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")

    async def aclose(self) -> None:
        if self._inner is not None:
            await self._inner.aclose()


def replay_orchestrator(
    path: Path | str,
    mode: Mode,
    *,
    liner_key: str = "replay",
    mfds_key: str = "replay",
    reuse_recorded: bool = False,
):
    """골든셋용 조립. 규칙표·주장 추출·판정은 실제 것을 쓰고 학술·식약처만 녹화 응답을 쓴다."""
    from healthcheck.adapters.liner.academic import LinerAcademicSearchClient
    from healthcheck.adapters.mfds.foodsafetykorea import FoodSafetyKoreaDataSource
    from healthcheck.api.app import build_fake_orchestrator

    transport = RecordReplayTransport(path, mode, reuse_recorded=reuse_recorded)
    client = httpx.AsyncClient(transport=transport)
    orchestrator = build_fake_orchestrator()
    # 녹화 재생은 호출 간격을 둘 필요가 없다.
    orchestrator.academic = LinerAcademicSearchClient(
        liner_key, client=client, max_qps=1000.0 if mode == "replay" else 2.0
    )
    # 사본을 쓰지 않고 API 응답을 녹화한다. 사본 날짜에 따라 결과가 달라지지 않게 한다.
    orchestrator.mfds = FoodSafetyKoreaDataSource(mfds_key, client=client, snapshot_path=None)
    return orchestrator, transport
