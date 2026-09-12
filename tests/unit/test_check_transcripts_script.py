"""검증 스크립트 테스트. 네트워크를 타지 않는다."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from healthcheck.adapters.youtube.transcript import TranscriptFetchError
from healthcheck.domain.types import ExtractedContent

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_transcripts.py"
spec = importlib.util.spec_from_file_location("check_transcripts", SCRIPT)
module = importlib.util.module_from_spec(spec)
# dataclass가 cls.__module__로 sys.modules를 찾으므로 먼저 등록해야 한다.
sys.modules["check_transcripts"] = module
spec.loader.exec_module(module)


@dataclass
class TrackStub:
    language_code: str
    is_generated: bool


class ApiStub:
    def __init__(self, tracks=None, error=None):
        self._tracks = tracks or []
        self._error = error

    def list(self, video_id: str):
        if self._error is not None:
            raise self._error
        return self._tracks


class ExtractorStub:
    def __init__(self, content=None, error=None):
        self._content = content
        self._error = error

    async def fetch(self, video_id: str):
        if self._error is not None:
            raise self._error
        return self._content


KOREAN_AUTO = [TrackStub("ko", True)]
CONTENT = ExtractedContent(
    text="홍삼이 면역력 증진에 도움을 줄 수 있다", has_transcript=True, confidence=0.65, language="ko"
)


async def inspect(target, extractor, api, claim_extractor=None):
    return await module.inspect(
        target, target, extractor=extractor, api=api, claim_extractor=claim_extractor
    )


async def test_자동생성_자막을_구분해_기록한다():
    row = await inspect("https://youtu.be/redginseng1", ExtractorStub(CONTENT), ApiStub(KOREAN_AUTO))

    assert row.status == "자막있음"
    assert row.caption_kind == "자동생성"
    assert row.chars == len(CONTENT.text)
    assert row.tracks == ["ko:자동"]


async def test_수동_자막도_구분한다():
    manual = ExtractedContent(text="홍삼", has_transcript=True, confidence=0.9, language="ko")
    row = await inspect("https://youtu.be/redginseng1", ExtractorStub(manual), ApiStub([TrackStub("ko", False)]))

    assert row.caption_kind == "수동"


async def test_한국어_자막이_없으면_따로_표시한다():
    empty = ExtractedContent(text="", has_transcript=False, confidence=0.0)
    row = await inspect(
        "https://youtu.be/dQw4w9WgXcQ", ExtractorStub(empty), ApiStub([TrackStub("en", False)])
    )

    assert row.status == "한국어없음"


async def test_차단은_자막없음과_구분한다():
    """차단은 다시 시도하면 달라진다. 통계에 섞이면 안 된다."""
    row = await inspect(
        "https://youtu.be/redginseng1", ExtractorStub(error=TranscriptFetchError("IpBlocked")), ApiStub(KOREAN_AUTO)
    )

    assert row.status == "차단"


async def test_주장까지_함께_뽑는다():
    from healthcheck.pipeline.step3_claims import RuleBasedClaimExtractor

    row = await inspect(
        "https://youtu.be/redginseng1",
        ExtractorStub(CONTENT),
        ApiStub(KOREAN_AUTO),
        RuleBasedClaimExtractor(),
    )

    assert row.claims and "홍삼" in row.claims[0]
    assert "범위내=True" in row.note


def test_목록_파일에서_주석과_설명을_읽는다(tmp_path):
    path = tmp_path / "videos.txt"
    path.write_text(
        "# 주석\n\nhttps://youtu.be/redginseng1  홍삼 영상\nhttps://youtu.be/garlic00001\n",
        encoding="utf-8",
    )

    import argparse

    targets = module.parse_targets(argparse.Namespace(targets=[str(path)]))

    assert targets == [
        ("https://youtu.be/redginseng1", "홍삼 영상"),
        ("https://youtu.be/garlic00001", "https://youtu.be/garlic00001"),
    ]


def test_유튜브_링크가_아니면_건너뛴다():
    import asyncio

    row = asyncio.run(inspect("그냥 문장입니다", ExtractorStub(CONTENT), ApiStub(KOREAN_AUTO)))
    assert row.status == "링크아님"


def test_샘플_목록_파일이_읽힌다():
    import argparse

    samples = Path(__file__).resolve().parents[2] / "samples" / "videos.txt"
    targets = module.parse_targets(argparse.Namespace(targets=[str(samples)]))

    assert len(targets) == 5
    assert all(target.startswith("https://") for target, _ in targets)
