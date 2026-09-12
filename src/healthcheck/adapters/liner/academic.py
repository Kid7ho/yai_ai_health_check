"""Liner Academic Search 실제 클라이언트.

확인된 스펙 (공개 문서 기준):
  POST https://platform.liner.com/api/v1/tools/search/scholar
  헤더   x-api-key, Content-Type: application/json
  본문   {"query": ..., "lang": ..., "date_range": ..., "max_results": 1~20}
         Scholar 모드는 country_code를 지원하지 않는다
  응답   {"requestId": ..., "results": [...], "totalCount": ...}
         results 항목: title, url, hostname, faviconUrl, description,
                       date, citationCount, authors, journal

설계에서 신경 쓴 것 세 가지.

1. 검색어를 영문으로 만든다
   학술 논문은 대부분 영어다. "홍삼 면역력"으로 검색하면 결과가 거의 없다.
   rules/ingredient_aliases.yaml의 영문 표기를 써서 질의를 만들고, 표기가
   없으면 한국어 그대로 보낸다.

2. 찬반 판정은 우리가 한다
   Liner 응답에는 stance 필드가 없다. 제목과 초록의 표현으로 판정하며
   그 표현 목록은 rules/stance_patterns.yaml에 있다. 거친 판정이므로
   어느 쪽에도 걸리지 않으면 NEUTRAL로 두고 뒷받침으로 세지 않는다.

3. 실패는 예외로 올린다
   ④ 검증 단계가 그 예외를 잡아 lookup_failed로 바꾸고, 판정 단계가
   UNKNOWN으로 흡수한다. 여기서 빈 결과를 돌려주면 "논문이 없다"와
   "조회에 실패했다"가 구분되지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from pathlib import Path

import httpx
import yaml

from healthcheck.domain.types import Claim, Paper

logger = logging.getLogger(__name__)

SCHOLAR_URL = "https://platform.liner.com/api/v1/tools/search/scholar"

RULES_DIR = Path(__file__).resolve().parents[4] / "rules"
DEFAULT_ALIASES_PATH = RULES_DIR / "ingredient_aliases.yaml"
DEFAULT_STANCE_PATH = RULES_DIR / "stance_patterns.yaml"

# 파이프라인 전체 예산이 10~20초다. 학술 검색과 식약처 조회를 동시에 보내므로
# 한쪽에 8초까지 준다. 콜백 URL 유효 시간 5분 안에는 넉넉히 들어온다.
DEFAULT_TIMEOUT = 8.0

# 효능 표기를 논문 제목과 맞출 때 쓰는 어간 길이.
# 영어 의학 용어는 앞 5글자가 같으면 같은 개념인 경우가 많다.
_STEM_LENGTH = 5
DEFAULT_MAX_RESULTS = 10


class AcademicSearchError(RuntimeError):
    """학술 검색 실패. ④ 검증 단계가 잡아 lookup_failed로 바꾼다."""


@lru_cache(maxsize=2)
def _load_query_terms(path: Path | str = DEFAULT_ALIASES_PATH) -> tuple[dict, dict]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return raw.get("영문명") or {}, raw.get("효능_대상_영문") or {}


@lru_cache(maxsize=2)
def _load_stance_terms(
    path: Path | str = DEFAULT_STANCE_PATH,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    refute = tuple(term.lower() for term in raw.get("반박_표현") or ())
    support = tuple(term.lower() for term in raw.get("뒷받침_표현") or ())
    excluded = tuple(term.lower() for term in raw.get("제외_표현") or ())
    return refute, support, excluded


def query_terms_of(claim: Claim) -> tuple[str, str]:
    """검색어를 만드는 데 쓴 (성분 표기, 효능 표기).

    관련성 검사에서 같은 표기를 다시 쓴다. 검색어와 검사 기준이 어긋나면
    관련 있는 논문을 버리거나 없는 논문을 통과시킨다.
    """
    ingredient_terms, target_terms = _load_query_terms()
    ingredient = ingredient_terms.get(claim.ingredient, claim.ingredient)

    # 긴 표기를 먼저 본다. '혈당'이 '혈당 조절'을 가로채지 않게 한다.
    for korean in sorted(target_terms, key=len, reverse=True):
        if korean in claim.claimed_effect:
            return ingredient, target_terms[korean]
    return ingredient, ""


def build_query(claim: Claim) -> str:
    """주장 하나를 학술 검색어로 바꾼다.

    성분과 효능 대상을 영문으로 옮겨 붙인다. 영문 표기가 없으면 한국어를
    그대로 쓴다. 결과가 안 나오는 것이 엉뚱한 결과가 나오는 것보다 낫다.
    """
    ingredient, effect = query_terms_of(claim)
    return f"{ingredient} {effect}".strip() if effect else ingredient


def is_relevant(paper_title: str, description: str | None, ingredient: str, effect: str) -> bool:
    """이 문헌이 주장의 근거가 될 수 있는가.

    Liner Scholar는 특허와 제조법 문헌도 함께 돌려준다. 실제 호출에서
    "블루베리 안토시아닌 추출 방법" 특허 세 건이 당뇨 주장의 뒷받침 논문으로
    세어졌다. 그래서 두 가지를 확인한다.

      1. 제조법·특허 문헌이 아닌가
      2. 주장의 효능 대상이 제목이나 초록에 실제로 나오는가

    2번이 특히 중요하다. 검색어에 효능을 넣어도 Liner는 성분만 맞는 문헌을
    돌려준다. 효능이 언급되지 않은 문헌은 그 효능에 대한 근거가 아니다.
    """
    refute_terms, support_terms, excluded_terms = _load_stance_terms()
    text = f"{paper_title} {description or ''}".lower()

    if any(term in text for term in excluded_terms):
        return False

    # 효능 대상을 특정하지 못한 주장은 성분 언급만으로 관련 있다고 본다.
    if not effect:
        return True

    # 'immune function'처럼 두 단어면 한 단어만 맞아도 관련으로 본다.
    # 또 논문 제목은 'immune'을 'immunity'로 쓰는 등 변형이 많으므로
    # 앞 5글자만 잘라 부분 문자열로 찾는다. immune/immunity, diabetes/diabetic,
    # hypertension/hypertensive가 같은 줄기로 묶인다.
    stems = [word[:_STEM_LENGTH] for word in effect.lower().split() if len(word) > 3]
    return any(stem in text for stem in stems)


def classify_stance(paper_title: str, description: str | None) -> str:
    """논문이 주장을 뒷받침하는가, 반박하는가.

    반박 표현을 먼저 본다. 제목에 부정문이 섞인 논문을 뒷받침으로 세면
    판정이 뒤집힌다. 어느 쪽에도 걸리지 않으면 NEUTRAL이다.
    """
    refute_terms, support_terms, _ = _load_stance_terms()
    text = f"{paper_title} {description or ''}".lower()

    if any(term in text for term in refute_terms):
        return "REFUTE"
    if any(term in text for term in support_terms):
        return "SUPPORT"
    return "NEUTRAL"


class LinerAcademicSearchClient:
    """Liner Scholar 엔드포인트를 호출한다."""

    def __init__(
        self,
        api_key: str,
        *,
        url: str = SCHOLAR_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("LINER_API_KEY가 없습니다. .env를 확인하세요.")
        self._api_key = api_key
        self._url = url
        self._timeout = timeout
        self._max_retries = max_retries
        self._client = client
        self._owns_client = client is None
        # 호출 횟수를 센다. Liner 토큰 비용을 지원받는 구조이므로
        # 캐시가 실제로 호출을 줄이는지 확인할 수 있어야 한다.
        self.call_count = 0

    async def search(
        self, claim: Claim, *, max_results: int = DEFAULT_MAX_RESULTS
    ) -> tuple[Paper, ...]:
        query = build_query(claim)
        payload = {
            "query": query,
            # 검색어를 영문으로 만들므로 영어권 결과를 받는다.
            "lang": "en",
            # max_results는 1~20만 허용된다.
            "max_results": max(1, min(max_results, 20)),
        }

        data = await self._post(payload, query=query)
        results = data.get("results") or []

        ingredient, effect = query_terms_of(claim)
        all_papers = tuple(_to_paper(item) for item in results if isinstance(item, dict))
        papers = tuple(
            paper
            for paper in all_papers
            if is_relevant(paper.title, paper.description, ingredient, effect)
        )

        logger.info(
            "학술 검색 완료 query=%r 받은결과=%d 관련=%d 뒷받침=%d 반박=%d",
            query,
            len(all_papers),
            len(papers),
            sum(1 for paper in papers if paper.stance == "SUPPORT"),
            sum(1 for paper in papers if paper.stance == "REFUTE"),
        )
        return papers

    async def _post(self, payload: dict, *, query: str) -> dict:
        client = self._ensure_client()
        headers = {"x-api-key": self._api_key, "Content-Type": "application/json"}

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await client.post(
                    self._url, json=payload, headers=headers, timeout=self._timeout
                )
            except httpx.HTTPError as error:
                last_error = error
                logger.warning("학술 검색 통신 오류 (%d회차): %s", attempt + 1, error)
            else:
                if response.status_code == 200:
                    return response.json()

                # 429는 호출 제한, 5xx는 서버 문제다. 둘만 재시도한다.
                # 401·403은 키 문제이므로 재시도해도 같은 결과다.
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = AcademicSearchError(
                        f"학술 검색 응답 {response.status_code}"
                    )
                    logger.warning(
                        "학술 검색 재시도 대상 응답 %d (%d회차)",
                        response.status_code,
                        attempt + 1,
                    )
                else:
                    # 본문에 키가 섞여 나올 수 있으므로 앞부분만 남긴다.
                    raise AcademicSearchError(
                        f"학술 검색 실패 {response.status_code}: {response.text[:200]}"
                    )

            if attempt < self._max_retries:
                await asyncio.sleep(0.5 * (attempt + 1))

        raise AcademicSearchError(f"학술 검색 실패: query={query!r}") from last_error

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owns_client = True
        self.call_count += 1
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


def _to_paper(item: dict) -> Paper:
    """Liner 응답 한 건을 우리 타입으로 옮긴다."""
    authors = item.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]

    title = str(item.get("title") or "").strip()
    description = item.get("description")

    return Paper(
        title=title,
        url=str(item.get("url") or ""),
        description=description,
        date=item.get("date"),
        citation_count=_as_int(item.get("citationCount")),
        authors=tuple(str(author) for author in authors),
        journal=item.get("journal"),
        stance=classify_stance(title, description),
    )


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
