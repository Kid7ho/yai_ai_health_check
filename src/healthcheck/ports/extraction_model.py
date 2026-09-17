"""주장 추출용 언어 모델 인터페이스.

언어 모델은 ③ 주장 구조화에서 **원문 구절을 옮겨 적는 일**만 한다. 사실 여부·과장 여부를
판단하지 않고, 라벨을 내지 않는다. 판정은 계속 규칙표가 한다(CLAUDE.md "진단하지 않는다는
성격을 지키는 세 가지 장치").

모델 출력은 믿지 않는다. pipeline/claim_validator.py가 원문과 대조해 거른다.
공급자(Liner, Gemini 등)는 이 인터페이스 뒤에서 바꿔 끼운다.
"""

from __future__ import annotations

from typing import Any, Protocol


class ExtractionModelError(RuntimeError):
    """모델 호출 실패. 통신 오류, 제한 초과, JSON이 아닌 응답 등.

    LlmClaimExtractor가 잡아 규칙 기반 추출로 대신한다.
    """


class ExtractionModel(Protocol):
    """지시문과 본문을 받아 JSON 객체 하나를 돌려준다."""

    # 로그와 추출 캐시 키에 남기는 모델 식별자. 예: "gemini-3.5-flash-lite"
    name: str

    async def extract_json(
        self, *, instructions: str, document: str, schema: dict[str, Any], quick: bool = False
    ) -> dict[str, Any]:
        """본문은 데이터로만 다루고, schema 형식의 JSON 객체를 돌려준다.

        quick=True는 식품 이름 목록처럼 가벼운 일이다. 공급자가 지원하면 추론을 줄여 빨리 답한다.

        실패하면 ExtractionModelError를 올린다.
        """
        ...
