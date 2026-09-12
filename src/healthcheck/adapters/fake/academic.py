"""학술 검색 클라이언트 Fake.

Liner Scholar 응답 필드(title, url, description, date, citationCount, authors,
journal)에 맞춰 만든다. stance는 Liner 응답에 없는 값이므로 클라이언트가
채우는 책임을 진다는 설계를 그대로 반영한다.

TODO(확인필요): 실제 응답 표본을 받지 못했다. 필드 모양만 맞춘 표본이다.
"""

from __future__ import annotations

from healthcheck.domain.types import Claim, Paper

# (성분, 효능 키워드) → 논문 목록
FAKE_PAPERS: dict[tuple[str, str], tuple[Paper, ...]] = {
    ("홍삼", "면역력"): (
        Paper(
            title="Effects of Korean Red Ginseng on immune function: a randomized trial",
            url="https://example.org/ginseng-immune",
            description="홍삼 섭취군에서 NK세포 활성이 유의하게 증가했다.",
            date="2019-04-01",
            citation_count=142,
            authors=("Kim", "Lee"),
            journal="Journal of Ginseng Research",
            stance="SUPPORT",
        ),
        Paper(
            title="Red ginseng supplementation and immune markers: a meta-analysis",
            url="https://example.org/ginseng-meta",
            date="2021-08-15",
            citation_count=88,
            journal="Nutrients",
            stance="SUPPORT",
        ),
    ),
    ("블루베리", "당뇨"): (
        Paper(
            title="Blueberry intake and glycemic control: systematic review",
            url="https://example.org/blueberry-glycemic",
            description="혈당 지표에 소폭 개선이 보고되었으나 당뇨 치료 효과는 확인되지 않았다.",
            date="2020-02-10",
            citation_count=57,
            journal="Diabetes Care",
            stance="REFUTE",
        ),
        Paper(
            title="No evidence that dietary anthocyanins reverse type 2 diabetes",
            url="https://example.org/anthocyanin-t2d",
            date="2022-06-30",
            citation_count=31,
            journal="BMJ Nutrition",
            stance="REFUTE",
        ),
        Paper(
            title="Anthocyanins and insulin sensitivity in adults",
            url="https://example.org/anthocyanin-insulin",
            date="2018-11-05",
            citation_count=24,
            stance="SUPPORT",
        ),
    ),
    ("마늘", "혈압"): (
        Paper(
            title="Garlic supplementation and blood pressure: meta-analysis of RCTs",
            url="https://example.org/garlic-bp",
            description="수축기 혈압이 평균 4mmHg 감소했다.",
            date="2016-09-20",
            citation_count=203,
            journal="Journal of Hypertension",
            stance="SUPPORT",
        ),
    ),
    ("루테인", "치매"): (
        Paper(
            title="Lutein and cognitive decline: inconclusive findings",
            url="https://example.org/lutein-cognition",
            date="2021-01-12",
            citation_count=19,
            stance="REFUTE",
        ),
    ),
}


class FakeAcademicSearchClient:
    """성분과 효능 키워드로 표에서 찾는다. 없으면 빈 결과를 돌려준다."""

    def __init__(self, papers: dict[tuple[str, str], tuple[Paper, ...]] | None = None) -> None:
        self._papers = FAKE_PAPERS if papers is None else papers
        self.calls: list[str] = []

    async def search(self, claim: Claim, *, max_results: int = 10) -> tuple[Paper, ...]:
        self.calls.append(f"{claim.ingredient}:{claim.claimed_effect}")
        for (ingredient, keyword), papers in self._papers.items():
            if ingredient == claim.ingredient and keyword in claim.claimed_effect:
                return papers[:max_results]
        return ()
