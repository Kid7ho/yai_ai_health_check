"""여러 주장 검증 테스트.

이전에는 본문에 먼저 나온 주장 하나만 검증했다. 영상 자막 한 편에서 주장 10건이
나왔는데 "홍삼 / 눈" 한 건만 확인했고, 뒤에 위험한 주장이 있어도 판정에 반영되지
않았다.
"""

from __future__ import annotations

from healthcheck.api.app import build_fake_orchestrator
from healthcheck.domain.labels import Label
from healthcheck.pipeline import step1_input
from healthcheck.pipeline.orchestrator import MAX_VERIFIED_CLAIMS, SEVERITY
from healthcheck.pipeline.step6_response import render


class CountingAcademic:
    def __init__(self, inner):
        self.inner = inner
        self.queries: list[str] = []

    async def search(self, claim, **kwargs):
        self.queries.append(f"{claim.ingredient}/{claim.claimed_effect}")
        return await self.inner.search(claim, **kwargs)


async def run(text: str):
    orchestrator = build_fake_orchestrator()
    orchestrator.academic = CountingAcademic(orchestrator.academic)
    result = await orchestrator.run(step1_input.classify(text))
    return orchestrator, result


async def test_뒤에_나온_주장도_검증한다():
    """앞 주장은 믿을 만하고 뒤 주장은 미인정인 글이다."""
    text = "홍삼이 면역력 증진에 도움을 줄 수 있습니다. 마늘이 혈압을 낮추는 데 좋다고 합니다."
    orchestrator, result = await run(text)

    assert len(result.claim_checks) == 2
    assert len(orchestrator.academic.queries) == 2


async def test_가장_엄한_결과를_쓴다():
    text = "홍삼이 면역력 증진에 도움을 줄 수 있습니다. 마늘이 혈압을 낮추는 데 좋다고 합니다."
    _, result = await run(text)

    labels = {check.claim.ingredient: check.verdict.label for check in result.claim_checks}
    assert labels["홍삼"] is Label.TRUSTWORTHY
    assert labels["마늘"] is Label.CAUTION
    assert result.verdict.label is Label.CAUTION
    assert result.primary_claim.ingredient == "마늘"


async def test_판정을_결정한_주장을_카드에_보여_준다():
    text = "홍삼이 면역력 증진에 도움을 줄 수 있습니다. 마늘이 혈압을 낮추는 데 좋다고 합니다."
    _, result = await run(text)

    description = render(result)["template"]["outputs"][0]["textCard"]["description"]
    assert "마늘" in description


async def test_검증은_최대_세_건까지만_한다():
    """주장마다 Liner를 호출한다. 토큰 비용 때문에 상한을 둔다."""
    text = " ".join(
        f"{name}이 {effect}에 도움을 줍니다."
        for name, effect in [("홍삼", "면역력"), ("루테인", "눈 건강"), ("마늘", "혈압"), ("밀크씨슬", "간 건강"), ("오메가3", "혈액순환")]
    )
    orchestrator, result = await run(text)

    assert len(result.claims.claims) >= 4
    assert len(result.claim_checks) == MAX_VERIFIED_CLAIMS
    assert len(orchestrator.academic.queries) == MAX_VERIFIED_CLAIMS


async def test_단정_표현을_먼저_검증한다():
    """상한에 걸릴 때 위험한 주장이 밀려나지 않게 한다."""
    text = (
        "홍삼이 면역력에 도움을 줍니다. 루테인이 눈 건강에 좋습니다. "
        "밀크씨슬이 간 건강에 도움을 줍니다. 마늘은 혈압을 반드시 낮춥니다."
    )
    _, result = await run(text)

    checked = [check.claim.ingredient for check in result.claim_checks]
    assert checked[0] == "마늘"


async def test_확인_못한_주장이_섞이면_믿을_만해요를_주지_않는다():
    """모든 주장이 믿을 만할 때만 믿을 만하다."""
    assert SEVERITY[Label.UNKNOWN] > SEVERITY[Label.TRUSTWORTHY]


async def test_주장이_하나면_이전과_같게_판정한다():
    _, result = await run("홍삼이 면역력 증진에 도움을 줄 수 있습니다")

    assert result.verdict.label is Label.TRUSTWORTHY
    assert len(result.claim_checks) == 1


async def test_주장이_없으면_검증하지_않는다():
    orchestrator, result = await run("어제 홍삼을 한 박스 샀습니다")

    assert result.claim_checks == ()
    assert orchestrator.academic.queries == []
