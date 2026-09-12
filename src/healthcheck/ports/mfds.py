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

TODO(확인필요): 부당광고 적발 정보의 전용 오픈API를 아직 특정하지 못했다.
행정처분결과(식품판매업) API가 대체 후보다. 필드명을 지어내지 않는다.
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
