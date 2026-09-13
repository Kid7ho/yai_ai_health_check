"""단축 링크 해석 인터페이스.

카카오톡으로 공유되는 링크에는 naver.me, kko.to, bit.ly 같은 단축 주소가 흔하다.
풀지 않으면 그 뒤의 실제 대상이 유튜브 영상인지 블로그 글인지 알 수 없다.
"""

from __future__ import annotations

from typing import Protocol


class LinkResolver(Protocol):
    """단축 링크를 실제 주소로 푼다. 풀 수 없으면 예외를 올린다."""

    async def resolve(self, url: str) -> str: ...
