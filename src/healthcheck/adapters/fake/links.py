"""단축 링크 해석 Fake."""

from __future__ import annotations

from healthcheck.adapters.web.links import LinkResolveError

FAKE_SHORT_LINKS: dict[str, str] = {
    "https://naver.me/ginseng1": "https://blog.naver.com/anarche69/224382558879",
    "https://kko.to/video01": "https://www.youtube.com/watch?v=redginseng1",
    "https://bit.ly/channel1": "https://www.youtube.com/@healthchannel",
}


class FakeLinkResolver:
    def __init__(self, links: dict[str, str] | None = None) -> None:
        self._links = FAKE_SHORT_LINKS if links is None else links
        self.calls: list[str] = []

    async def resolve(self, url: str) -> str:
        self.calls.append(url)
        if url not in self._links:
            raise LinkResolveError(f"Fake에 없는 단축 링크: {url}")
        return self._links[url]
