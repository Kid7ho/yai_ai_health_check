"""④-A 논문 입장 분류 — 언어 모델.

표현 목록 분류(adapters/liner/academic.py classify_stance)는 초록 어디에든 "did not", "no significant"가
있으면 반박으로 셌다. 실측에서 대조군 설명, 부작용 없음, 다른 지표의 결과 문장이 반박으로 세어져
홍삼·피로처럼 뒷받침 논문이 많은 주장이 조심하세요로 떨어졌다.

언어 모델은 논문마다 SUPPORT / REFUTE / NONE 하나를 고르고, 근거 문장을 초록에서 그대로 옮긴다.
지키는 선은 셋이다.
  1. 판정은 계속 규칙표가 한다. 모델은 논문 한 편의 입장만 고르고, 뒷받침·반박 수는 코드가 센다
  2. 근거 문장이 초록에 없으면 중립으로 떨어뜨린다. 지어낸 근거로 판정이 움직이지 않는다
  3. 모델에 보내는 것은 성분·효능의 영문 검색어와 공개 논문뿐이다. 사용자가 보낸 원문은 보내지 않는다

모델 호출이 실패하면 표현 목록 분류로 대신한다.

반박은 한 번 더 확인한다(verifier). 반박 하나가 믿을 만해요를 조심하세요로 바꾸는데, 첫 분류(추론 none)가 몸속 칼슘
신호·다른 질병·배경 설명 문장을 반박으로 골랐다. 반박 후보만 추론 low로 다시 보내 성분·섭취·같은 효능·이 논문의 결과·
방향을 따로 적게 하고, 하나라도 반박이 아니라고 하면 중립으로 둔다. 확인 호출이 실패하면 첫 분류의 반박을 그대로 둔다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from healthcheck.domain.types import Paper
from healthcheck.ports.extraction_model import ExtractionModel, ExtractionModelError

logger = logging.getLogger(__name__)

RULES_DIR = Path(__file__).resolve().parents[3] / "rules"
DEFAULT_PROMPT_PATH = RULES_DIR / "stance_prompt.yaml"

# 한 번에 보내는 논문 수. 한 주장에 관련 논문이 최대 10편이라 두 번으로 나눠 동시에 보낸다.
BATCH_SIZE = 5
# 논문 한 편 초록의 상한. Liner 응답 초록은 중앙값 1,357자다.
ABSTRACT_CHARS = 1_800
# 근거 문장이 이보다 짧으면 대조를 믿지 않는다("did not" 한 토막 같은 것).
MIN_EVIDENCE_CHARS = 20

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "papers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "paper": {"type": "integer"},
                    "stance": {"type": "string", "enum": ["SUPPORT", "REFUTE", "NONE"]},
                    "evidence": {"type": "string"},
                },
                "required": ["paper", "stance", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["papers"],
    "additionalProperties": False,
}


VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "papers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "paper": {"type": "integer"},
                    "same_ingredient": {"type": "boolean"},
                    "eaten_or_given": {"type": "boolean"},
                    "same_target": {"type": "boolean"},
                    "own_result": {"type": "boolean"},
                    "direction": {"type": "string", "enum": ["no_effect", "worse", "better", "mixed"]},
                },
                "required": ["paper", "same_ingredient", "eaten_or_given", "same_target", "own_result", "direction"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["papers"],
    "additionalProperties": False,
}


@lru_cache(maxsize=4)
def load_prompt(path: Path | str = DEFAULT_PROMPT_PATH) -> tuple[str, str]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return str(raw["version"]), str(raw["지시문"]).strip()


@lru_cache(maxsize=4)
def load_verify_prompt(path: Path | str = DEFAULT_PROMPT_PATH) -> str:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return str(raw["반박_확인_지시문"]).strip()


def normalize_text(text: str) -> str:
    """대조용 정규화. 대소문자, 공백, 따옴표·하이픈 변형을 맞춘다."""
    text = text.lower()
    text = re.sub(r"[‐‑‒–—−]", "-", text)
    text = re.sub(r"[“”″]", '"', text)
    text = re.sub(r"[‘’′]", "'", text)
    return re.sub(r"\s+", " ", text).strip()


def evidence_in_paper(evidence: str, paper: Paper) -> bool:
    """근거 문장이 제목이나 초록에 그대로 있는가. 모델이 앞뒤 문장 부호만 바꾼 것은 허용한다."""
    quote = normalize_text(evidence).strip(" .;:,\"'")
    if len(quote) < MIN_EVIDENCE_CHARS:
        return False
    source = normalize_text(f"{paper.title} {paper.description or ''}")
    return quote in source


class LlmStanceClassifier:
    """주장(성분, 효능) 하나에 대한 논문들의 입장을 언어 모델로 고른다."""

    def __init__(
        self,
        model: ExtractionModel,
        *,
        prompt_path: Path | str = DEFAULT_PROMPT_PATH,
        batch_size: int = BATCH_SIZE,
        samples: int = 1,
        verifier: ExtractionModel | None = None,
    ) -> None:
        self._model = model
        self._version, self._instructions = load_prompt(prompt_path)
        # 반박 후보를 다시 확인하는 모델. 없으면 첫 분류의 반박을 그대로 쓴다.
        self._verifier = verifier
        self._verify_instructions = load_verify_prompt(prompt_path) if verifier is not None else ""
        self._batch_size = max(1, batch_size)
        # 같은 묶음을 여러 번 동시에 보내 과반이 고른 입장만 쓴다. Liner는 temperature·seed를 적용하지 않아
        # 같은 논문의 입장이 호출마다 달라진다. 과반이 없으면 NEUTRAL이다.
        self._samples = max(1, samples)

    @property
    def name(self) -> str:
        suffix = f"x{self._samples}" if self._samples > 1 else ""
        verify = f"+verify:{self._verifier.name}" if self._verifier is not None else ""
        return f"llm:{self._model.name}{suffix}{verify}:{self._version}"

    async def classify(self, *, ingredient: str, effect: str, papers: tuple[Paper, ...]) -> tuple[Paper, ...]:
        """모델이 고른 입장으로 바꾼 논문 목록. 실패한 묶음은 원래 입장(표현 목록 분류)을 그대로 둔다."""
        if not papers or not effect:
            return papers
        batches = [papers[i : i + self._batch_size] for i in range(0, len(papers), self._batch_size)]
        results = await asyncio.gather(
            *(self._classify_batch(ingredient, effect, batch) for batch in batches), return_exceptions=True
        )
        classified: list[Paper] = []
        for batch, result in zip(batches, results):
            if isinstance(result, BaseException):
                logger.warning("논문 입장 분류 실패, 표현 목록 분류를 씁니다: %s", result)
                classified.extend(batch)
            else:
                classified.extend(result)
        if self._verifier is not None:
            classified = await self._verify_refutations(ingredient, effect, classified)
        return tuple(classified)

    async def _verify_refutations(self, ingredient: str, effect: str, papers: list[Paper]) -> list[Paper]:
        """반박 후보를 다시 확인해 반박이 아닌 것을 중립으로 바꾼다. 확인이 실패한 후보는 반박으로 둔다."""
        candidates = [i for i, paper in enumerate(papers) if paper.stance == "REFUTE"]
        batches = [candidates[i : i + self._batch_size] for i in range(0, len(candidates), self._batch_size)]
        results = await asyncio.gather(
            *(
                self._verifier.extract_json(  # type: ignore[union-attr]
                    instructions=self._verify_instructions,
                    document=_document(ingredient, effect, tuple(papers[i] for i in batch)),
                    schema=VERIFY_SCHEMA,
                )
                for batch in batches
            ),
            return_exceptions=True,
        )
        checked = list(papers)
        for batch, output in zip(batches, results):
            if isinstance(output, BaseException):
                logger.warning("반박 확인 실패, 첫 분류의 반박을 그대로 둡니다: %s", output)
                continue
            for position, holds in enumerate(refutation_checks(output, len(batch))):
                if holds is False:
                    index = batch[position]
                    logger.info("반박 확인에서 반박이 아니라고 봐 중립으로 둡니다: %s", papers[index].title[:60])
                    checked[index] = replace(papers[index], stance="NEUTRAL")
        return checked

    async def _classify_batch(self, ingredient: str, effect: str, batch: tuple[Paper, ...]) -> tuple[Paper, ...]:
        document = _document(ingredient, effect, batch)
        outputs = await asyncio.gather(
            *(
                self._model.extract_json(instructions=self._instructions, document=document, schema=SCHEMA, sample=i)
                for i in range(self._samples)
            ),
            return_exceptions=True,
        )
        votes: list[list[str]] = []
        for output in outputs:
            if isinstance(output, BaseException):
                logger.warning("논문 입장 분류 호출 하나 실패: %s", output)
                continue
            try:
                votes.append(parse_output(output, batch))
            except ExtractionModelError as error:
                logger.warning("논문 입장 분류 출력 하나를 버립니다: %s", error)
        if not votes:
            raise ExtractionModelError("논문 입장 분류 호출이 모두 실패했습니다")
        stances = [majority([vote[i] for vote in votes], self._samples) for i in range(len(batch))]
        return tuple(replace(paper, stance=stance) for paper, stance in zip(batch, stances))


def majority(stances: list[str], samples: int) -> str:
    """보낸 호출 수의 과반이 고른 입장. 실패한 호출도 분모에 넣는다. 과반이 없으면 NEUTRAL이다."""
    for stance in ("SUPPORT", "REFUTE"):
        if stances.count(stance) * 2 > samples:
            return stance
    return "NEUTRAL"


def parse_output(output: dict[str, Any], batch: tuple[Paper, ...]) -> list[str]:
    """모델 출력을 논문 순서대로 SUPPORT / REFUTE / NEUTRAL로 옮긴다.

    빠진 논문, 근거 문장이 초록에 없는 뒷받침·반박은 NEUTRAL이다. 판단 불가를 어느 쪽으로도 세지 않는다.
    """
    stances = ["NEUTRAL"] * len(batch)
    entries = output.get("papers") if isinstance(output, dict) else None
    if not isinstance(entries, list):
        raise ExtractionModelError("입장 분류 출력 형식이 예상과 다릅니다")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index = entry.get("paper")
        if not isinstance(index, int) or not 1 <= index <= len(batch):
            continue
        stance = entry.get("stance")
        if stance not in ("SUPPORT", "REFUTE"):
            continue
        paper = batch[index - 1]
        if not evidence_in_paper(str(entry.get("evidence") or ""), paper):
            logger.info("입장 근거 문장이 초록에 없어 중립으로 둡니다: %s", paper.title[:60])
            continue
        stances[index - 1] = stance
    return stances


def refutation_checks(output: dict[str, Any], count: int) -> list[bool | None]:
    """반박 확인 출력을 후보 순서대로 옮긴다. True는 반박 유지, False는 중립, None은 확인 못 함(반박 유지).

    성분이 같고, 먹거나 준 결과이고, 같은 효능이고, 이 논문의 결과이고, 방향이 효과 없음·악화·엇갈림일 때만 반박이다.
    엇갈림을 반박에 넣는 것은 첫 분류의 반박 정의("근거가 부족하거나 일관되지 않다")와 같다.
    """
    checks: list[bool | None] = [None] * count
    entries = output.get("papers") if isinstance(output, dict) else None
    if not isinstance(entries, list):
        return checks
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index = entry.get("paper")
        if not isinstance(index, int) or not 1 <= index <= count:
            continue
        checks[index - 1] = (
            entry.get("same_ingredient") is True
            and entry.get("eaten_or_given") is True
            and entry.get("same_target") is True
            and entry.get("own_result") is True
            and entry.get("direction") in ("no_effect", "worse", "mixed")
        )
    return checks


def _document(ingredient: str, effect: str, batch: tuple[Paper, ...]) -> str:
    lines = [f"Claim: {ingredient} is beneficial for {effect}.", "<papers>"]
    for number, paper in enumerate(batch, start=1):
        abstract = (paper.description or "").strip()[:ABSTRACT_CHARS]
        lines.append(f"[paper {number}]\nTitle: {paper.title}\nAbstract: {abstract or '(none)'}")
    lines.append("</papers>")
    return "\n\n".join(lines)
