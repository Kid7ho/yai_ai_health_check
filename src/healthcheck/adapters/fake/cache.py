"""메모리 캐시 구현.

테스트용 Fake이면서, 인프로세스 단일 서버에서는 실제 구현으로도 쓸 수 있다.
Redis로 교체할 때는 같은 인터페이스를 만족하는 구현을 새로 만들면 된다.
"""

from __future__ import annotations

from healthcheck.domain.types import CheckResult, ClaimSet


class InMemoryVerdictCache:
    """판정 캐시. 규칙표 버전을 키에 포함한다.

    규칙표가 바뀌면 과거 판정은 더 이상 유효하지 않다. 버전을 키에 넣으면
    무효화를 따로 구현하지 않아도 이전 버전 항목이 조회되지 않는다.
    """

    def __init__(self) -> None:
        self._store: dict[str, CheckResult] = {}
        self.hits = 0
        self.misses = 0

    async def get(self, key: str, *, rules_version: str) -> CheckResult | None:
        found = self._store.get(self._key(key, rules_version))
        if found is None:
            self.misses += 1
            return None
        self.hits += 1
        return found

    async def set(self, key: str, value: CheckResult, *, rules_version: str) -> None:
        self._store[self._key(key, rules_version)] = value

    @staticmethod
    def _key(key: str, rules_version: str) -> str:
        return f"{rules_version}|{key}"


class InMemoryExtractionCache:
    """추출·구조화 캐시. 규칙표 버전과 무관하게 유지된다."""

    def __init__(self) -> None:
        self._store: dict[str, ClaimSet] = {}
        self.hits = 0
        self.misses = 0

    async def get(self, content_hash: str) -> ClaimSet | None:
        found = self._store.get(content_hash)
        if found is None:
            self.misses += 1
            return None
        self.hits += 1
        return found

    async def set(self, content_hash: str, value: ClaimSet) -> None:
        self._store[content_hash] = value
