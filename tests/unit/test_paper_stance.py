"""언어 모델 논문 입장 분류 단위 테스트. 네트워크를 타지 않는다."""

from __future__ import annotations

import httpx

from healthcheck.adapters.fake.extraction_model import FakeExtractionModel
from healthcheck.adapters.liner.academic import LinerAcademicSearchClient
from healthcheck.api.app import _build_stance_classifier
from healthcheck.config import Settings
from healthcheck.domain.types import Claim, Paper
from healthcheck.pipeline.paper_stance import LlmStanceClassifier, evidence_in_paper, majority, parse_output

SUPPORTING = Paper(
    title="Korean red ginseng reduces fatigue in adults",
    url="https://example.org/a",
    description="In a randomized trial, red ginseng significantly reduced fatigue scores compared with placebo. "
    "No significant differences in adverse events were observed.",
    stance="REFUTE",  # 표현 목록 분류는 부작용 문장의 no significant 때문에 반박으로 셌다
)
UNRELATED = Paper(
    title="Ginsenoside content of steamed ginseng",
    url="https://example.org/b",
    description="We measured ginsenoside content by HPLC.",
    stance="NEUTRAL",
)


def output(*entries: tuple[int, str, str]) -> dict:
    return {"papers": [{"paper": i, "stance": s, "evidence": e} for i, s, e in entries]}


def test_근거_문장이_초록에_있으면_모델이_고른_입장을_쓴다():
    stances = parse_output(
        output((1, "SUPPORT", "red ginseng significantly reduced fatigue scores compared with placebo."), (2, "NONE", "")),
        (SUPPORTING, UNRELATED),
    )
    assert stances == ["SUPPORT", "NEUTRAL"]


def test_근거_문장이_초록에_없으면_중립으로_떨어뜨린다():
    """지어낸 근거로 뒷받침·반박 수가 움직이면 안 된다."""
    stances = parse_output(output((1, "REFUTE", "Red ginseng had no effect on fatigue in this trial.")), (SUPPORTING,))
    assert stances == ["NEUTRAL"]


def test_짧은_토막이나_범위_밖_번호는_세지_않는다():
    assert evidence_in_paper("did not", SUPPORTING) is False
    stances = parse_output(output((3, "SUPPORT", "red ginseng significantly reduced fatigue scores")), (SUPPORTING,))
    assert stances == ["NEUTRAL"]


def test_대소문자와_공백_차이는_같은_문장으로_본다():
    assert evidence_in_paper("RED GINSENG  significantly reduced\nfatigue scores", SUPPORTING) is True


def test_과반이_없으면_중립이다():
    assert majority(["SUPPORT", "SUPPORT", "REFUTE"], 3) == "SUPPORT"
    assert majority(["SUPPORT", "REFUTE"], 3) == "NEUTRAL"
    # 실패한 호출도 분모에 들어간다. 세 번 중 한 번만 답했으면 과반이 아니다.
    assert majority(["SUPPORT"], 3) == "NEUTRAL"


async def test_모델이_실패하면_표현_목록_분류를_그대로_둔다():
    classifier = LlmStanceClassifier(FakeExtractionModel(fail=True))
    papers = await classifier.classify(ingredient="Korean red ginseng", effect="fatigue", papers=(SUPPORTING,))
    assert papers[0].stance == "REFUTE"


async def test_모델에는_검색어와_논문만_보낸다():
    """사용자가 보낸 원문 구절은 모델에 보내지 않는다."""
    model = FakeExtractionModel(
        responder=lambda _: output((1, "SUPPORT", "red ginseng significantly reduced fatigue scores"))
    )
    classifier = LlmStanceClassifier(model, batch_size=5)
    papers = await classifier.classify(ingredient="Korean red ginseng", effect="fatigue", papers=(SUPPORTING,))
    assert papers[0].stance == "SUPPORT"
    assert model.calls[0].startswith("Claim: Korean red ginseng is beneficial for fatigue.")
    assert "홍삼" not in model.calls[0]


async def test_논문을_묶음으로_나눠_보낸다():
    model = FakeExtractionModel(responder=lambda _: output())
    classifier = LlmStanceClassifier(model, batch_size=2)
    papers = await classifier.classify(ingredient="garlic", effect="cholesterol", papers=(UNRELATED,) * 5)
    assert len(model.calls) == 3
    assert len(papers) == 5


class RecordingClassifier:
    name = "recording"

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, int]] = []

    async def classify(self, *, ingredient: str, effect: str, papers: tuple[Paper, ...]) -> tuple[Paper, ...]:
        self.seen.append((ingredient, effect, len(papers)))
        return tuple(Paper(title=p.title, url=p.url, description=p.description, stance="SUPPORT") for p in papers)


async def test_학술_검색은_관련_논문만_분류기에_넘긴다():
    response = {
        "results": [
            {"title": "No evidence that red ginseng affects immune function", "url": "https://example.org/b"},
            {"title": "Method for preparing Korean red ginseng immune powder", "url": "https://example.org/c"},
        ]
    }
    classifier = RecordingClassifier()
    client = LinerAcademicSearchClient(
        "test-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response))),
        max_retries=0,
        stance_classifier=classifier,
    )
    claim = Claim(ingredient="홍삼", claimed_effect="면역력 증진에 도움을", intensity="SUGGESTIVE", span=(0, 2))
    papers = await client.search(claim)
    assert classifier.seen == [("Korean red ginseng", "immune function", 1)]
    assert [p.stance for p in papers] == ["SUPPORT"]


def test_설정이_rule이면_분류기를_만들지_않는다():
    assert _build_stance_classifier(Settings(liner_api_key="k")) is None
    built = _build_stance_classifier(Settings(liner_api_key="k", paper_stance_classifier="liner"))
    assert built is not None and built.name.startswith("llm:liner-mark-1.0/none:")
