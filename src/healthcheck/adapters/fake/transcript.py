"""자막 추출기 Fake.

빈 껍데기가 아니다. 골든 테스트셋을 통과시킬 수 있을 만큼 현실적으로 만든다.

특히 기본 동작이 "자막 없음"이다. 발표 자료가 문제로 제시한 생성형 AI 제작
영상은 사람이 올린 자막이 없는 경우가 많다. Fake의 기본값을 '자막 있음'으로
두면 실제 서비스에서 UNKNOWN이 쏟아지는 상황을 개발 중에 못 본다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from healthcheck.domain.types import ExtractedContent


@dataclass(frozen=True, slots=True)
class FakeTranscript:
    text: str
    confidence: float = 0.9
    language: str = "ko"


# 영상 ID → 자막. 골든 케이스와 수동 확인에 쓰는 표본이다.
# TODO(확인필요): 실제 유튜브 자막 표본을 아직 받지 못했다. 팀이 자막 2~3건을
# 수집하면 이 표를 실제 문장으로 교체한다.
# 키는 실제 유튜브 영상 ID와 같은 11자로 맞춘다. 길이가 다르면 ① 단계가
# 유튜브 링크로 인식하지 못해 자막을 찾으러 가지도 않는다.
FAKE_TRANSCRIPTS: dict[str, FakeTranscript] = {
    # 질병 완치 주장. 가장 위험한 유형이다.
    "blueberry01": FakeTranscript(
        "여러분 냉동 블루베리 아시죠. 이걸 매일 아침에 드시면 당뇨를 완치시킨다고 합니다. "
        "제 지인도 이걸 먹고 혈당이 정상으로 돌아왔어요."
    ),
    # 고시형 원료의 인정 범위 안 주장.
    "redginseng1": FakeTranscript(
        "홍삼은 면역력 증진에 도움을 줄 수 있다고 알려져 있습니다. "
        "6년근 홍삼을 꾸준히 드시는 분들이 많습니다."
    ),
    # 인정 원료인데 인정되지 않은 효능을 붙인 경우.
    "lutein00001": FakeTranscript(
        "루테인은 눈 건강에 도움을 줄 수 있습니다. 그런데 루테인이 치매도 예방한다고 합니다."
    ),
    # 국가 인정이 없는 성분에 대한 주장.
    "garlic00001": FakeTranscript("마늘이 혈압을 낮추는 데 좋다고 합니다."),
    # 자동 생성 자막이라 신뢰도가 낮은 경우. 규칙 R-010이 판정을 보류한다.
    "lowconf0001": FakeTranscript("마늘 혈압 좋다 어쩌구", confidence=0.3),
    # 의약품 이야기. 규칙 R-001이 거절한다.
    "outofscope1": FakeTranscript("당뇨약 끊고 이 즙만 드시면 인슐린 안 맞아도 됩니다."),
}


class FakeTranscriptExtractor:
    """자막 표에서 찾아 돌려준다. 표에 없으면 자막 없음으로 답한다."""

    def __init__(self, transcripts: dict[str, FakeTranscript] | None = None) -> None:
        self._transcripts = FAKE_TRANSCRIPTS if transcripts is None else transcripts
        self.calls: list[str] = []

    async def fetch(self, video_id: str) -> ExtractedContent:
        self.calls.append(video_id)
        found = self._transcripts.get(video_id)

        if found is None:
            # 자막 없음은 오류가 아니다. 판정 보류로 이어지는 정상 경로다.
            return ExtractedContent(
                text="", has_transcript=False, confidence=0.0, content_hash=None
            )

        return ExtractedContent(
            text=found.text,
            has_transcript=True,
            confidence=found.confidence,
            language=found.language,
            content_hash=hashlib.sha256(found.text.encode("utf-8")).hexdigest()[:32],
        )
