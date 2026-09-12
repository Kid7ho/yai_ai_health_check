"""스킬 엔드포인트 인증.

스킬 서버는 공개 https 주소로 노출해야 한다. 인증이 없으면 주소를 아는
누구나 파이프라인을 호출할 수 있다. 그러면 세 가지 문제가 생긴다.

  · Liner 토큰이 남의 요청에 쓰인다. 산학협력으로 지원받는 비용이다
  · 큐가 차서 어르신 요청이 밀린다
  · 저장소에 우리 서비스와 무관한 기록이 쌓여 목표 지표가 오염된다

두 가지 방법을 함께 쓴다. 둘 다 선택이며, 최소 하나는 켜는 것을 권한다.

1. 헤더 토큰
   오픈빌더 스킬 설정에서 헤더를 지정할 수 있으면 이 방법이 깔끔하다.
   TODO(확인필요): 오픈빌더가 임의 헤더를 지원하는지 확인하지 못했다.
   지원하지 않으면 2번만 쓴다.

2. 비밀 경로
   엔드포인트 경로 자체에 추측하기 어려운 문자열을 넣는다. 헤더를 지정할
   수 없어도 쓸 수 있고, 주소가 https로 암호화되어 전송된다.
   다만 서버 접근 로그에는 경로가 남으므로 헤더 토큰보다 약하다.

비교는 hmac.compare_digest로 한다. 문자열 비교 시간 차이로 토큰을 알아내는
공격을 막는다.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

DEFAULT_HEADER = "X-Skill-Token"


@dataclass(frozen=True, slots=True)
class SkillAuth:
    header_name: str = DEFAULT_HEADER
    token: str | None = field(default=None, repr=False)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def verify(self, request: Request) -> None:
        """토큰이 설정되어 있으면 검사한다. 없으면 통과시킨다.

        검사에 실패하면 401을 돌려주고 파이프라인을 돌리지 않는다. 카카오
        정상 요청은 헤더를 달고 오므로 영향받지 않는다.
        """
        if not self.enabled:
            return

        supplied = request.headers.get(self.header_name, "")
        if not hmac.compare_digest(supplied, self.token or ""):
            logger.warning(
                "스킬 토큰 불일치. 요청을 처리하지 않는다. 헤더=%s 제공=%s",
                self.header_name,
                "있음" if supplied else "없음",
            )
            raise HTTPException(status_code=401, detail="인증되지 않은 요청입니다.")


def warn_if_unprotected(auth: SkillAuth, skill_path: str, debug_ui: bool) -> None:
    """보호되지 않은 상태로 기동하면 경고를 남긴다.

    조용히 열어 두지 않는다. 터널로 노출하는 순간 공개 엔드포인트가 된다.
    """
    if not auth.enabled and skill_path == "/skill":
        logger.warning(
            "스킬 엔드포인트가 보호되지 않았다. SKILL_AUTH_TOKEN을 설정하거나 "
            "SKILL_PATH를 추측하기 어려운 값으로 바꿔라."
        )
    if debug_ui:
        logger.warning(
            "디버그 화면이 켜져 있다. 외부에 노출할 때는 DEBUG_UI=0으로 꺼라."
        )
