"""판정 라벨 정의.

라벨은 4개뿐이다. 어르신에게 보여줄 한국어 문구까지 여기서 함께 관리한다.
네 번째 라벨 UNKNOWN이 존재하는 이유는 보수적 기본값 때문이다.
어느 규칙에도 확실히 맞지 않으면 오판하지 않고 판정을 보류한다.
"""

from __future__ import annotations

from enum import StrEnum


class Label(StrEnum):
    """4단계 신호등 라벨."""

    TRUSTWORTHY = "TRUSTWORTHY"  # 믿을 만해요
    CAUTION = "CAUTION"  # 조심하세요
    WARNING = "WARNING"  # 주의하세요
    UNKNOWN = "UNKNOWN"  # 알 수 없어요


# 어르신에게 노출되는 한국어 문구. 발표 자료의 표기와 일치시킨다.
LABEL_TEXT: dict[Label, str] = {
    Label.TRUSTWORTHY: "믿을 만해요",
    Label.CAUTION: "조심하세요",
    Label.WARNING: "주의하세요",
    Label.UNKNOWN: "알 수 없어요",
}
