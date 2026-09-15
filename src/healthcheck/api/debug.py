"""디버그 채팅 화면.

카카오톡 채널에 붙이기 전에 실제 데이터를 넣어 보며 동작을 확인하는 도구다.
제품 화면이 아니다. 어르신에게 보여 줄 화면은 카카오 말풍선이다.

지시서의 "프론트엔드를 만들지 말 것"과 어긋나 보일 수 있으나, 이것은
개발용 디버그 도구이며 배포 대상이 아니다. DEBUG_UI=0으로 끌 수 있고,
판정 로직을 건드리지 않고 파이프라인을 그대로 호출한다.

동기로 실행한다. 실제 서비스는 즉시 ack 후 콜백이지만, 여기서는 중간
산출물을 한 번에 보여 주는 편이 디버깅에 낫다. 대신 실제로 카카오에
나갈 ack 페이로드와 콜백 페이로드를 응답에 함께 담아, 이식할 때 쓸 수
있게 한다.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from healthcheck.domain.types import CheckResult
from healthcheck.pipeline import step1_input
from healthcheck.pipeline.orchestrator import Orchestrator
from healthcheck.pipeline.step6_response import ack_response, render, render_reason

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])

PAGE_PATH = Path(__file__).parent / "static" / "debug.html"


@router.get("")
async def page() -> FileResponse:
    return FileResponse(PAGE_PATH, media_type="text/html; charset=utf-8")


@router.post("/check")
async def check(request: Request) -> dict:
    """발화 하나를 파이프라인에 넣고 결과와 중간 산출물을 전부 돌려준다."""
    body = await request.json()
    utterance = str(body.get("utterance") or "").strip()
    mode = "fake" if body.get("mode") == "fake" else "live"

    if not utterance:
        return {"error": "발화가 비었습니다."}

    orchestrators: dict[str, Orchestrator] = request.app.state.debug_orchestrators
    orchestrator = orchestrators.get(mode) or orchestrators["fake"]

    share_input = step1_input.classify(utterance)
    trace: dict[str, float] = {}

    try:
        result = await orchestrator.run(share_input, trace=trace)
    except Exception as error:  # 디버그 화면에서는 원인을 그대로 보여 준다
        logger.exception("디버그 실행 실패")
        return {"error": f"{type(error).__name__}: {error}"}

    image_base_url = getattr(request.app.state, "image_base_url", None)
    return {
        "mode": mode,
        "adapters": {
            "transcript": type(orchestrator.transcript).__name__,
            "academic": type(orchestrator.academic).__name__,
            "mfds": type(orchestrator.mfds).__name__,
        },
        "input": {
            "kind": share_input.kind,
            "video_id": share_input.video_id,
            "url": share_input.url,
            "cache_key": step1_input.cache_key(share_input),
        },
        "timings_ms": trace,
        "content": _content_of(result),
        "claims": _claims_of(result),
        "mfds": asdict(result.mfds) if result.mfds else None,
        "academic": _academic_of(result),
        "verdict": _verdict_of(result),
        "claim_checks": [
            {
                "ingredient": check.claim.ingredient,
                "claimed_effect": check.claim.claimed_effect,
                "label": check.verdict.label.value,
                "rule_id": check.verdict.rule_id,
            }
            for check in result.claim_checks
        ],
        "from_cache": result.from_cache,
        "retryable_failure": result.retryable_failure,
        "rules_version": orchestrator.ruleset.version,
        # 카카오로 이식할 때 그대로 쓰는 페이로드
        "ack_payload": ack_response(),
        "callback_payload": render(result, image_base_url=image_base_url),
        "reason_payload": render_reason(result, image_base_url=image_base_url),
    }


def _content_of(result: CheckResult) -> dict | None:
    content = result.content
    if content is None:
        return None
    return {
        "has_transcript": content.has_transcript,
        "confidence": content.confidence,
        "language": content.language,
        "length": len(content.text),
        "content_hash": content.content_hash,
        "text": content.text,
    }


def _claims_of(result: CheckResult) -> dict | None:
    claims = result.claims
    if claims is None:
        return None
    return {
        "in_scope": claims.in_scope,
        "disease_cure_expression": claims.disease_cure_expression,
        "extraction_failed": claims.extraction_failed,
        "items": [asdict(claim) for claim in claims.claims],
        "brands": list(claims.brands),
        "authority_roles": list(claims.authority_roles),
    }


def _academic_of(result: CheckResult) -> dict | None:
    academic = result.academic
    if academic is None:
        return None
    return {
        "supporting": academic.supporting,
        "refuting": academic.refuting,
        "lookup_failed": academic.lookup_failed,
        "papers": [asdict(paper) for paper in academic.papers],
    }


def _verdict_of(result: CheckResult) -> dict:
    verdict = result.verdict
    return {
        "label": verdict.label.value,
        "rule_id": verdict.rule_id,
        "rules_version": verdict.rules_version,
        "reasons": list(verdict.reasons),
        "inputs": asdict(verdict.inputs) if verdict.inputs else None,
    }
