"""캐시 인터페이스. 2단 구조다.

경로당과 복지관에서는 같은 영상이 여러 어르신에게 돌기 때문에 캐시 적중률이
높다. 적중률이 높아지면 Liner 토큰 비용과 응답 지연이 함께 줄어든다.

1단 VerdictCache — 영상 ID 또는 원문 해시 기준. 판정 결과를 그대로 담는다.
  5초 안에 동기 응답을 할 수 있는 경로는 여기뿐이다.
  규칙표 버전이 바뀌면 이 캐시는 무효화된다.

2단 ExtractionCache — 자막 콘텐츠 해시 기준. 추출·구조화 결과를 담는다.
  콘텐츠 해시는 자막을 가져온 뒤에만 계산할 수 있어서 동기 경로에 쓸 수 없다.
  규칙표 버전이 바뀌어도 이 캐시는 유지된다.
"""

from __future__ import annotations

from typing import Protocol

from healthcheck.domain.types import CheckResult, ClaimSet


class VerdictCache(Protocol):
    """판정 캐시. 규칙표 버전이 키에 포함된다."""

    async def get(self, key: str, *, rules_version: str) -> CheckResult | None: ...

    async def set(self, key: str, value: CheckResult, *, rules_version: str) -> None: ...


class ExtractionCache(Protocol):
    """추출·구조화 캐시. 규칙표 버전과 무관하다."""

    async def get(self, content_hash: str) -> ClaimSet | None: ...

    async def set(self, content_hash: str, value: ClaimSet) -> None: ...
