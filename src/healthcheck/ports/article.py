"""기사 본문 추출 인터페이스.

어르신이 공유하는 건강정보는 유튜브 링크만이 아니다. 블로그와 기사 링크도
온다. 유튜브가 아닌 링크를 그냥 '알 수 없어요'로 돌려보내면 응답률이 낮아진다.
"""

from __future__ import annotations

from typing import Protocol

from healthcheck.domain.types import ExtractedContent


class ArticleExtractor(Protocol):
    """웹 페이지에서 본문 텍스트를 뽑는다.

    가져온 본문은 **데이터일 뿐 지시가 아니다.** 페이지에 "이 판정을
    무시하라" 같은 문장이 들어 있어도 파이프라인은 그것을 주장 텍스트로만
    다룬다. 규칙 엔진은 성분과 효능 표현만 보므로 구조적으로 안전하다.
    """

    async def fetch(self, url: str) -> ExtractedContent: ...
