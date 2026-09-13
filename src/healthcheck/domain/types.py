"""파이프라인 단계 간에 주고받는 타입 전부.

단계 사이에는 이 모듈의 타입으로만 값을 넘긴다. dict를 그대로 흘리지 않는다.
모든 타입은 frozen dataclass다. 중간 산출물을 그대로 로깅할 수 있어야 하고,
한 단계가 앞 단계의 결과를 조용히 바꿔치기하는 일을 막기 위해서다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from healthcheck.domain.labels import Label

# ─────────────────────────────────────────────────────────────
# 리터럴 타입
# ─────────────────────────────────────────────────────────────

# ① 입력 수신 단계의 링크 종류 판별 결과
#   YOUTUBE        영상 하나를 가리키는 유튜브 링크
#   YOUTUBE_OTHER  유튜브 링크지만 영상이 아니다 (채널, 재생목록, 검색 결과)
#   OTHER_LINK     기사·블로그 등 유튜브가 아닌 링크. 단축 링크도 여기서 출발한다
#   PLAIN_TEXT     링크가 없는 텍스트
InputKind = Literal["YOUTUBE", "YOUTUBE_OTHER", "OTHER_LINK", "PLAIN_TEXT"]

# 주장하는 효능이 식약처 인정 기능성 문구와 얼마나 맞는가
#   EXACT          완전 일치
#   PARTIAL        일부만 겹치거나 인정 범위보다 넓게 말함
#   MISMATCH       인정 문구와 전혀 다름
#   NOT_APPLICABLE 인정 원료가 아니어서 비교 자체가 불가
MatchLevel = Literal["EXACT", "PARTIAL", "MISMATCH", "NOT_APPLICABLE"]

# 식약처 기능성 원료 인정 상태
#   INDIVIDUAL     개별인정형 원료 (기능성 원료인정 현황 I-0040에 등재)
#   NOTIFIED       고시형 원료 (건강기능식품 공전 등재)
#   NOT_RECOGNIZED 인정된 기능성 원료가 아님
#   UNKNOWN        조회 실패 또는 성분을 특정하지 못함
Recognition = Literal["INDIVIDUAL", "NOTIFIED", "NOT_RECOGNIZED", "UNKNOWN"]

# 자막 품질. 규칙표의 transcript_quality 조건과 값이 1:1로 대응한다.
#   none 자막 없음 / low 신뢰도 미달 / ok 사용 가능
TranscriptQuality = Literal["none", "low", "ok"]

# 표현 강도. ③ 주장 구조화가 판정한다.
#   ASSERTIVE   단정 ("낫는다", "없어진다")
#   SUGGESTIVE  완화 ("도움이 된다", "좋다")
#   UNCERTAIN   추측 ("한다고 한다")
Intensity = Literal["ASSERTIVE", "SUGGESTIVE", "UNCERTAIN"]


# ─────────────────────────────────────────────────────────────
# ① 입력 수신
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ShareInput:
    """어르신이 채널로 공유한 원문 하나."""

    raw_text: str
    kind: InputKind
    # 유튜브 링크일 때만 채워진다. 캐시 1단(판정 캐시)의 키로 쓰인다.
    video_id: str | None = None
    url: str | None = None
    # 입력이 길이 상한을 넘어 잘렸는가.
    truncated: bool = False
    # 단축 링크라서 실제 주소를 풀어야 종류를 확정할 수 있는가.
    needs_resolution: bool = False


# ─────────────────────────────────────────────────────────────
# ② 내용 추출
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExtractedContent:
    """자막 또는 원문 텍스트.

    이번 MVP는 자막이 있는 영상만 지원한다. 음성 인식(ASR)은 범위 밖이다.
    자막이 없으면 has_transcript=False로 두고 규칙 R-010이 UNKNOWN으로 끝낸다.
    """

    text: str
    has_transcript: bool
    # 0.0~1.0. 자동 생성 자막이거나 글자가 깨졌으면 낮게 준다.
    confidence: float
    language: str | None = None
    # 캐시 2단(추출 캐시)의 키. 같은 자막이면 같은 값이 나와야 한다.
    content_hash: str | None = None
    # 본문의 출처. captions | asr | article | title_only | text
    # 제목만으로 판정한 경우를 로그와 근거 화면에서 구분하기 위해 남긴다.
    source: str | None = None


# ─────────────────────────────────────────────────────────────
# ③ 주장 구조화
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Claim:
    """자막에서 뽑아낸 주장 하나. (성분·제품명, 주장 효능, 표현 강도)"""

    # 정규화된 성분명 또는 제품명. rules/ingredient_aliases.yaml을 거친 값이다.
    ingredient: str
    claimed_effect: str
    intensity: Intensity
    # 원문에서 이 주장이 나온 위치. 근거 화면에서 원문을 되짚어 보여주기 위해 남긴다.
    span: tuple[int, int]
    # 성분명 사전에 없는 표기를 그대로 보존한다. 사전 보강 작업의 입력이 된다.
    raw_ingredient: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimSet:
    """한 건의 입력에서 뽑은 주장 전체."""

    claims: tuple[Claim, ...]
    # 서비스 범위(음식·식품·건강기능식품) 안의 내용인가.
    # 의약품·시술·질병 진단이면 False. 규칙 R-001이 거절한다.
    in_scope: bool
    # 질병 치료·완치 표현이 하나라도 있는가. 규칙 R-020, R-021의 입력이다.
    disease_cure_expression: bool
    # 식품 표시·광고법이 금지하는 광고 표현이 있는가. 규칙 R-031의 입력이다.
    # 식약처 적발 기록 조회가 아니라 표현 매칭 결과다. 둘을 구분해 둔다.
    prohibited_ad_expression: bool = False
    # 규칙 기반 추출이 주장을 하나도 못 뽑았으면 True. UNKNOWN으로 이어진다.
    extraction_failed: bool = False


# ─────────────────────────────────────────────────────────────
# ④ 이중 검증
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Paper:
    """Liner Scholar 응답 한 건. 필드명은 실제 API 응답을 따른다."""

    title: str
    url: str
    description: str | None = None
    date: str | None = None
    citation_count: int | None = None
    authors: tuple[str, ...] = ()
    journal: str | None = None
    # 이 논문이 주장을 뒷받침하는가, 반박하는가, 판단 불가인가
    stance: Literal["SUPPORT", "REFUTE", "NEUTRAL"] = "NEUTRAL"


@dataclass(frozen=True, slots=True)
class AcademicEvidence:
    """④-A 학술 검증 결과."""

    supporting: int
    refuting: int
    papers: tuple[Paper, ...] = ()
    # 검색 자체가 실패했으면 True. 판정을 UNKNOWN으로 떨어뜨리는 근거가 된다.
    lookup_failed: bool = False


@dataclass(frozen=True, slots=True)
class MfdsEvidence:
    """④-B 식약처 공적 기록 조회 결과."""

    recognition: Recognition
    # 인정된 기능성 내용 원문. I-0040의 FNCLTY_CN 값에 해당한다.
    functionality_text: str | None
    match_level: MatchLevel
    # 부당광고 적발 이력에 해당하는 제품·표현인가
    ad_violation_hit: bool = False
    # 인정번호. I-0040의 HF_FNCLTY_MTRAL_RCOGN_NO 값에 해당한다.
    recognition_no: str | None = None
    lookup_failed: bool = False
    # 같은 성분이 '다른 효능'으로 인정받은 내용. 주장 효능은 미인정이지만
    # 성분 자체는 인정 원료일 때 채워진다. 어르신에게 "이 성분은 간 건강으로는
    # 인정됐지만 혈압 기능성은 인정되지 않았습니다"라고 설명하는 재료다.
    other_recognitions: tuple[str, ...] = ()


# ─────────────────────────────────────────────────────────────
# ⑤ 판정
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VerdictInput:
    """규칙 엔진이 보는 평평한 구조.

    규칙표 YAML의 조건 이름과 이 클래스의 필드 이름이 1:1로 대응한다.
    의료 자문의가 YAML만 보고 검수할 수 있어야 하므로, 여기에 없는 값을
    규칙표에서 참조하는 일이 생기면 규칙 로딩 단계에서 오류로 잡는다.
    """

    in_scope: bool
    transcript_quality: TranscriptQuality
    disease_cure_expression: bool
    # 식약처 부당광고 적발 이력에 해당하는가. 전용 API를 확보하지 못해
    # 현재는 항상 False다. R-020과 R-030은 그래서 발동하지 않는다.
    ad_violation_hit: bool
    recognition: Recognition
    functionality_match: MatchLevel
    supporting_papers: int
    refuting_papers: int
    # 법령이 금지하는 광고 표현을 쓰고 있는가. 우리가 표현으로 판단한 값이며
    # 적발 기록 조회와는 다르다.
    #
    # 기본값을 둔 이유는 골든 케이스 호환이다. 팀이 손으로 적은 판정입력에
    # 새 항목이 없어도 테스트가 깨지지 않아야 한다. 기본값 False는 "위반
    # 표현 없음"이므로, 값을 빠뜨리면 이 규칙이 발동하지 않을 뿐 잘못된
    # 경고가 생기지는 않는다.
    prohibited_ad_expression: bool = False


@dataclass(frozen=True, slots=True)
class Verdict:
    """⑤ 판정 결과.

    어떤 판정이 왜 나왔는지 나중에 역추적할 수 있어야 한다.
    그래서 라벨만 담지 않고 규칙 버전과 매칭된 규칙 ID를 함께 기록한다.
    """

    label: Label
    rule_id: str
    rules_version: str
    # 규칙표에 적힌 설명과 근거. 어르신에게 보여주는 "왜 그런가요" 문구의 재료다.
    reasons: tuple[str, ...] = ()
    # 판정에 쓰인 입력값 전체. 로그에 그대로 남긴다.
    inputs: VerdictInput | None = None
    evidence: tuple[str, ...] = ()


# ─────────────────────────────────────────────────────────────
# 파이프라인 전체 결과
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ClaimCheck:
    """주장 하나를 검증한 결과."""

    claim: Claim
    verdict: Verdict
    academic: AcademicEvidence | None = None
    mfds: MfdsEvidence | None = None


@dataclass(frozen=True, slots=True)
class CheckResult:
    """한 건의 확인 요청에 대한 최종 산출물.

    ⑥ 응답 생성이 이 값만 보고 카카오 카드를 만든다.
    """

    verdict: Verdict
    claims: ClaimSet | None = None
    academic: AcademicEvidence | None = None
    mfds: MfdsEvidence | None = None
    content: ExtractedContent | None = None
    # 캐시에서 나온 결과인가. 5초 동기 응답 경로를 탔는지 확인하는 지표다.
    from_cache: bool = False
    # 일시적 실패로 나온 결과인가. 자막 서버 차단처럼 다시 시도하면 달라질 수
    # 있는 경우다. 이 결과는 캐시하지 않고, 어르신에게도 '알 수 없어요'가 아니라
    # 잠시 뒤 다시 보내 달라고 안내한다.
    retryable_failure: bool = False
    # 판정 대신 어르신에게 알려야 할 안내. 예: 영상이 아닌 유튜브 링크.
    # 값이 있으면 응답 생성이 판정 카드 대신 안내 문구를 보낸다.
    notice: str | None = None
    # 판정을 결정한 주장. 여러 주장 중 가장 엄한 결과를 낸 것이다.
    primary_claim: Claim | None = None
    # 검증한 주장 전부와 각각의 판정. 역추적과 근거 화면에 쓴다.
    claim_checks: tuple[ClaimCheck, ...] = ()
    source_urls: tuple[str, ...] = field(default_factory=tuple)
