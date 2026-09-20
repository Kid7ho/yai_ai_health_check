"""저장소 인터페이스.

SQLite로 시작하지만 Postgres로 교체할 수 있어야 한다. 그래서 저장 구조가
아니라 필요한 동작만 여기에 적는다.

저장하는 이유는 셋이다.
  1. 누적 확인 건수와 오정보 유형 수가 사업 목표 지표다
  2. 어떤 판정이 왜 나왔는지 나중에 역추적해야 한다
  3. 캐시가 재시작해도 살아 있어야 Liner 토큰을 아낀다

개인정보는 보존 기간을 넘기면 지운다. 무엇을 얼마나 두는지는 README의
표에 정리했고, 그 정책을 이 인터페이스가 강제한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from healthcheck.domain.labels import Label


@dataclass(frozen=True, slots=True)
class CheckRecord:
    """확인 요청 한 건의 저장 단위.

    개인정보와 판정 결과를 한 행에 담되, 보존 기간이 다른 항목은 만료 시각을
    따로 둔다. 만료된 항목만 비우고 행은 남긴다. 그래야 지표 집계가 유지된다.
    """

    check_id: str
    created_at: datetime

    # 판정 결과 — 무기한 보존. 개인을 식별하지 않는다.
    label: Label
    rule_id: str
    rules_version: str
    input_kind: str
    from_cache: bool

    # 판정 근거 요약 — 오정보 유형 집계에 쓴다.
    ingredient: str | None = None
    claimed_effect: str | None = None
    intensity: str | None = None
    recognition: str | None = None
    supporting_papers: int = 0
    refuting_papers: int = 0
    transcript_quality: str | None = None

    # 개인정보 또는 원문 — 보존 기간이 지나면 비운다.
    raw_text: str | None = None
    transcript_text: str | None = None
    user_key: str | None = None
    video_id: str | None = None
    url: str | None = None
    content_hash: str | None = None

    # 만료 시각. None이면 이미 비워졌거나 처음부터 저장하지 않은 항목이다.
    text_expires_at: datetime | None = None
    user_key_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Metrics:
    """사업 목표 지표.

    발표 자료의 목표 성과 지표가 참여 어르신 수, 누적 확인 건수, 오정보 유형
    수다. 참여자 수는 사용자 키 기준이므로 보존 기간이 지나면 셀 수 없다.
    그래서 확인 건수와 오정보 유형 수를 주 지표로 둔다.
    """

    total_checks: int
    by_label: dict[str, int]
    # 오정보 유형 수. (규칙 ID, 성분) 조합의 가짓수로 센다.
    misinformation_types: int
    cache_hits: int
    distinct_users: int
    rules_versions: dict[str, int] = field(default_factory=dict)
    # 성과 공유회에서 쓸 집계. 개인을 식별하는 값은 담지 않는다.
    # 입력 종류(YOUTUBE·OTHER_LINK·PLAIN_TEXT), 날짜별 건수, 많이 확인한 성분 순위
    by_input_kind: dict[str, int] = field(default_factory=dict)
    daily: dict[str, int] = field(default_factory=dict)
    top_ingredients: list[tuple[str, int]] = field(default_factory=list)


class CheckRepository(Protocol):
    """확인 기록 저장소."""

    async def save(self, record: CheckRecord) -> None: ...

    async def metrics(self) -> Metrics: ...

    async def purge_expired(self, now: datetime | None = None) -> dict[str, int]:
        """보존 기간이 지난 항목을 비운다.

        행을 지우지 않고 만료된 열만 비운다. 지표 집계가 유지되어야 하기
        때문이다. 사용자 키는 비우는 대신 해시로 바꿔, 같은 사람인지는
        구분할 수 있지만 누구인지는 알 수 없게 한다.
        """
        ...
