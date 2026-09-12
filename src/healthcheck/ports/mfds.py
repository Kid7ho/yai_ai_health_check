"""④-B 공적 기록 확인 — 식약처 데이터 소스 인터페이스.

확인된 실제 데이터:
  - 건강기능식품 기능성 원료인정 현황, 식품안전나라 오픈API 서비스ID I-0040
    컬럼: HF_FNCLTY_MTRAL_RCOGN_NO(인정번호), PRMS_DT(인정일자), BSSH_NM(업체명),
          INDUTY_NM(업종), ADDR(주소), APLC_RAWMTRL_NM(신청원료명),
          FNCLTY_CN(기능성 내용), DAY_INTK_CN(1일 섭취량),
          IFTKN_ATNT_MATR_CN(섭취시 주의사항)

TODO(확인필요): I-0040은 개별인정형 원료만 담는다. 홍삼·프로바이오틱스 같은
고시형 원료는 건강기능식품 공전이 출처이며 별도 데이터가 필요하다.
고시형 목록을 확보하기 전까지 NOTIFIED 판정은 내릴 수 없다.

부당광고 적발 정보는 전용 오픈API를 찾지 못했다. 확인 결과는 이렇다.
  · 식품안전나라 오픈API에 행정처분·부당광고 서비스가 없다. I0490(회수·판매
    중지)의 회수 사유는 규격 위반이고 광고 위반이 아니다.
  · 공공데이터포털의 행정처분결과(식품판매업)가 유일한 대체 후보인데,
    활용신청 전에는 응답 필드명을 확인할 수 없다. 필드명을 지어내지 않으므로
    그 어댑터는 만들지 않았다.

그래서 ad_violation_hit은 항상 False다. 대신 법령이 금지하는 광고 표현을
③ 구조화에서 확인해 규칙 R-031이 판정한다. 적발 이력 조회와는 다른 값이며
VerdictInput에서도 별도 항목으로 구분한다.
"""

from __future__ import annotations

from typing import Protocol

from healthcheck.domain.types import Claim, MfdsEvidence


class MfdsDataSource(Protocol):
    """성분·효능 한 쌍에 대해 국가 기록을 조회한다.

    인정 여부 판정과 기능성 문구 일치도 판정을 함께 돌려준다.
    일치도 판정(EXACT / PARTIAL / MISMATCH)의 기준은 구현체가 정하고,
    판정 근거가 되는 원문 문구를 functionality_text에 남긴다.
    """

    async def lookup(self, claim: Claim) -> MfdsEvidence: ...
