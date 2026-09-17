"""④-A 학술 검증 — Liner Academic Search 클라이언트 인터페이스.

실제 엔드포인트는 POST https://platform.liner.com/api/v1/tools/search/scholar 이고
인증 헤더는 x-api-key다. 요청 파라미터는 query, lang, date_range, max_results(1~20)이며
응답 필드는 title, url, description, date, citationCount, authors, journal이다.

API 키가 아직 승인 대기 중이므로 이번 단계에서 실제 호출 코드는 만들지 않는다.
검색 실패는 예외로 올리되, 파이프라인은 그것을 UNKNOWN 판정으로 흡수해야 한다.
"""

from __future__ import annotations

from typing import Protocol

from healthcheck.domain.types import Claim, Paper


class AcademicSearchClient(Protocol):
    """하나의 주장에 대해 뒷받침·반박 논문을 찾는다.

    stance 판정(SUPPORT / REFUTE / NEUTRAL)을 어디서 할지가 설계 쟁점이다.
    Liner 응답에는 stance 필드가 없다. 이번 단계에서는 클라이언트 구현체가
    제목과 초록을 보고 채우는 책임을 지고, 그 근거를 남긴다.
    """

    async def search(self, claim: Claim, *, max_results: int = 10) -> tuple[Paper, ...]: ...


class PaperStanceClassifier(Protocol):
    """관련 논문마다 주장(성분, 효능)에 대한 입장을 고른다.

    학술 검색 클라이언트가 관련성 검사를 마친 논문을 넘긴다. 입장만 바꾼 같은 순서의 목록을 돌려준다.
    판단할 수 없는 논문은 NEUTRAL로 두고, 호출이 실패한 논문은 받은 입장을 그대로 둔다.
    """

    name: str

    async def classify(self, *, ingredient: str, effect: str, papers: tuple[Paper, ...]) -> tuple[Paper, ...]: ...
