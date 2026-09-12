"""판정 결과를 저장 단위로 바꾼다.

보존 기간 정책을 여기 한곳에서 적용한다. 만료 시각을 호출자가 정하게 두면
어디선가 빠뜨린다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from healthcheck.domain.types import CheckResult, ShareInput
from healthcheck.ports.repository import CheckRecord

# README의 저장 데이터 표와 같은 값이다.
TEXT_RETENTION_DAYS = 90
USER_KEY_RETENTION_DAYS = 30


def build_record(
    *,
    share_input: ShareInput,
    result: CheckResult,
    user_key: str | None = None,
    text_retention_days: int = TEXT_RETENTION_DAYS,
    user_key_retention_days: int = USER_KEY_RETENTION_DAYS,
    now: datetime | None = None,
) -> CheckRecord:
    now = now or datetime.now(timezone.utc)
    verdict = result.verdict
    claims = result.claims
    primary = claims.claims[0] if claims and claims.claims else None

    return CheckRecord(
        check_id=uuid.uuid4().hex,
        created_at=now,
        label=verdict.label,
        rule_id=verdict.rule_id,
        rules_version=verdict.rules_version,
        input_kind=share_input.kind,
        from_cache=result.from_cache,
        ingredient=primary.ingredient if primary else None,
        claimed_effect=primary.claimed_effect if primary else None,
        intensity=primary.intensity if primary else None,
        recognition=result.mfds.recognition if result.mfds else None,
        supporting_papers=result.academic.supporting if result.academic else 0,
        refuting_papers=result.academic.refuting if result.academic else 0,
        transcript_quality=verdict.inputs.transcript_quality if verdict.inputs else None,
        raw_text=share_input.raw_text or None,
        transcript_text=result.content.text if result.content and result.content.text else None,
        user_key=user_key,
        video_id=share_input.video_id,
        url=share_input.url,
        content_hash=result.content.content_hash if result.content else None,
        text_expires_at=now + timedelta(days=text_retention_days),
        user_key_expires_at=(
            now + timedelta(days=user_key_retention_days) if user_key else None
        ),
    )
