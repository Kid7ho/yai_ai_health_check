"""식약처 데이터 소스 Fake.

실제 오픈API I-0040의 컬럼 의미에 맞춰 만든다.
  APLC_RAWMTRL_NM          신청원료명
  FNCLTY_CN                기능성 내용
  HF_FNCLTY_MTRAL_RCOGN_NO 인정번호

TODO(확인필요): 아래 값은 실제 데이터가 아니다. 컬럼의 의미와 값의 모양만
맞춰 놓은 표본이다. 실제 데이터를 받으면 기능성 문구를 원문으로 교체하고
성분명 표기를 APLC_RAWMTRL_NM 값과 대조해야 한다.

TODO(확인필요): 고시형 원료(홍삼, 프로바이오틱스 등)는 I-0040에 없다.
건강기능식품 공전이 출처이며 별도 데이터가 필요하다. 여기서는 고시형으로
표시해 두지만, 실제 서비스에서 NOTIFIED 판정을 내리려면 그 데이터가 먼저
있어야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from healthcheck.domain.types import Claim, MfdsEvidence, Recognition


@dataclass(frozen=True, slots=True)
class FakeRecognition:
    """인정 원료 한 건."""

    recognition: Recognition
    functionality_text: str
    recognition_no: str | None
    # 이 기능성으로 볼 수 있는 효능 대상. 일치도 판정에 쓴다.
    covered_targets: tuple[str, ...]


# 성분(표준명) → 인정 정보
FAKE_RECOGNITIONS: dict[str, FakeRecognition] = {
    "홍삼": FakeRecognition(
        recognition="NOTIFIED",
        functionality_text="면역력 증진에 도움을 줄 수 있음",
        recognition_no=None,  # 고시형은 개별 인정번호가 없다
        covered_targets=("면역력",),
    ),
    "프로바이오틱스": FakeRecognition(
        recognition="NOTIFIED",
        functionality_text="유산균 증식 및 유해균 억제, 배변활동 원활에 도움을 줄 수 있음",
        recognition_no=None,
        covered_targets=("장",),
    ),
    "루테인": FakeRecognition(
        recognition="NOTIFIED",
        functionality_text="노화로 인해 감소될 수 있는 망막의 색소밀도 유지에 도움을 줄 수 있음",
        recognition_no=None,
        covered_targets=("눈",),
    ),
    "밀크씨슬": FakeRecognition(
        recognition="INDIVIDUAL",
        functionality_text="간 건강에 도움을 줄 수 있음",
        recognition_no="제2004-12호",
        covered_targets=("간",),
    ),
    "오메가3": FakeRecognition(
        recognition="NOTIFIED",
        functionality_text="혈중 중성지질 개선·혈행 개선에 도움을 줄 수 있음",
        recognition_no=None,
        covered_targets=("혈액순환", "콜레스테롤"),
    ),
}

# 부당광고 적발 이력에 해당하는 (성분, 표현) 조합
# TODO(확인필요): 부당광고 적발 정보의 전용 오픈API를 특정하지 못했다.
# 행정처분결과(식품판매업) API가 대체 후보다.
FAKE_AD_VIOLATIONS: tuple[tuple[str, str], ...] = (
    ("블루베리", "당뇨"),
    ("마늘", "고혈압"),
)


class FakeMfdsDataSource:
    """인정 표와 적발 표를 조회한다."""

    def __init__(
        self,
        recognitions: dict[str, FakeRecognition] | None = None,
        violations: tuple[tuple[str, str], ...] | None = None,
    ) -> None:
        self._recognitions = FAKE_RECOGNITIONS if recognitions is None else recognitions
        self._violations = FAKE_AD_VIOLATIONS if violations is None else violations
        self.calls: list[str] = []

    async def lookup(self, claim: Claim) -> MfdsEvidence:
        self.calls.append(claim.ingredient)

        ad_hit = any(
            ingredient == claim.ingredient and keyword in claim.claimed_effect
            for ingredient, keyword in self._violations
        )

        found = self._recognitions.get(claim.ingredient)
        if found is None:
            return MfdsEvidence(
                recognition="NOT_RECOGNIZED",
                functionality_text=None,
                match_level="NOT_APPLICABLE",
                ad_violation_hit=ad_hit,
            )

        level = _match_level(claim, found)
        if level == "MISMATCH":
            # 실제 구현과 같은 규칙을 따른다. 인정 여부는 (성분, 효능) 쌍이다.
            return MfdsEvidence(
                recognition="NOT_RECOGNIZED",
                functionality_text=None,
                match_level="NOT_APPLICABLE",
                ad_violation_hit=ad_hit,
                other_recognitions=(found.functionality_text,),
            )

        return MfdsEvidence(
            recognition=found.recognition,
            functionality_text=found.functionality_text,
            match_level=level,
            ad_violation_hit=ad_hit,
            recognition_no=found.recognition_no,
        )


def _match_level(claim: Claim, found: FakeRecognition) -> str:
    """주장 효능이 인정 기능성 문구와 얼마나 맞는가.

    실제 구현에서는 기능성 문구 원문과 주장 문구를 비교해야 한다.
    여기서는 인정 기능성이 덮는 효능 대상이 주장에 들어 있는지로 판단한다.

    표현 강도를 함께 본다. 인정 범위에 드는 효능이라도 단정 표현이면
    완전 일치로 보지 않는다. 인정 문구는 모두 '도움을 줄 수 있음'이라는
    완화 표현이기 때문이다.
    """
    covered = any(target in claim.claimed_effect for target in found.covered_targets)

    if not covered:
        return "MISMATCH"
    if claim.intensity == "ASSERTIVE":
        return "PARTIAL"
    return "EXACT"
