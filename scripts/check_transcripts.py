#!/usr/bin/env python3
"""유튜브 자막 확보 현황 검증 스크립트.

영상 목록을 넣으면 자막이 있는지, 사람이 올린 것인지 자동 생성인지,
글자 수가 얼마인지를 표로 뽑는다. 팀이 실제 영상 표본을 모을 때 쓴다.

이 스크립트가 답하려는 질문은 둘이다.
  1. 어르신이 받는 영상에 자막이 실제로 얼마나 있는가
     발표 자료는 "생성형 AI 제작 영상"을 문제로 제시한다. 그런 영상에
     자막이 없으면 이번 MVP는 대부분 '알 수 없어요'로 답한다.
  2. 자동 생성 자막에서 규칙 기반 추출이 주장을 뽑아내는가
     --claims를 주면 뽑아낸 주장까지 함께 보여 준다. 네트워크를 더 쓰지 않는다.

사용법
    python scripts/check_transcripts.py samples/videos.txt
    python scripts/check_transcripts.py samples/videos.txt --claims
    python scripts/check_transcripts.py --json 결과.json https://youtu.be/xxxxxxxxxxx
    python scripts/check_transcripts.py samples/videos.txt --interval 3

주의: 같은 IP에서 짧은 시간에 몰아 부르면 YouTube가 차단한다. 기본 간격은
2초이고, 차단이 감지되면 즉시 멈춘다. 목록이 길면 --interval을 키운다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from youtube_transcript_api import YouTubeTranscriptApi  # noqa: E402

from healthcheck.adapters.youtube.transcript import (  # noqa: E402
    TranscriptFetchError,
    YouTubeTranscriptExtractor,
)
from healthcheck.pipeline import step1_input  # noqa: E402
from healthcheck.pipeline.step3_claims import RuleBasedClaimExtractor  # noqa: E402


@dataclass
class Row:
    label: str
    video_id: str | None
    status: str  # 자막있음 | 자막없음 | 한국어없음 | 차단 | 링크아님 | 조회실패
    caption_kind: str = "-"  # 수동 | 자동생성 | -
    language: str = "-"
    chars: int = 0
    confidence: float = 0.0
    tracks: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)
    note: str = ""


def parse_targets(args: argparse.Namespace) -> list[tuple[str, str]]:
    """(원본 문자열, 설명) 목록. 파일과 명령줄 인자를 모두 받는다."""
    targets: list[tuple[str, str]] = []

    for raw in args.targets:
        path = Path(raw)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                targets.append((parts[0], parts[1].strip() if len(parts) > 1 else parts[0]))
        else:
            targets.append((raw, raw))

    return targets


async def inspect(
    target: str,
    label: str,
    *,
    extractor: YouTubeTranscriptExtractor,
    api: YouTubeTranscriptApi,
    claim_extractor: RuleBasedClaimExtractor | None,
) -> Row:
    share = step1_input.classify(target)

    if not share.video_id:
        return Row(label=label, video_id=None, status="링크아님", note=f"kind={share.kind}")

    row = Row(label=label, video_id=share.video_id, status="조회실패")

    # 어떤 자막 트랙이 있는지 먼저 본다. 본문 조회와 차단 지점이 다르다.
    try:
        row.tracks = [
            f"{t.language_code}:{'자동' if t.is_generated else '수동'}" for t in api.list(share.video_id)
        ]
    except Exception as error:
        row.status = "자막없음"
        row.note = type(error).__name__
        return row

    try:
        content = await extractor.fetch(share.video_id)
    except TranscriptFetchError as error:
        row.status = "차단"
        row.note = str(error).splitlines()[0][:60]
        return row

    if not content.has_transcript:
        row.status = "한국어없음" if row.tracks else "자막없음"
        return row

    row.status = "자막있음"
    row.language = content.language or "-"
    row.chars = len(content.text)
    row.confidence = content.confidence
    row.caption_kind = "자동생성" if content.confidence < 0.9 else "수동"

    if claim_extractor is not None:
        result = claim_extractor.extract(content.text)
        row.note = f"범위내={result.in_scope} 완치표현={result.disease_cure_expression}"
        row.claims = [
            f"{claim.ingredient} / {claim.claimed_effect[:34]} / {claim.intensity}"
            for claim in result.claims[:5]
        ]

    return row


def print_table(rows: list[Row], show_claims: bool) -> None:
    print()
    print(f"{'영상':38s} {'상태':10s} {'자막':8s} {'언어':5s} {'글자':>6s}  트랙")
    print("-" * 100)
    for row in rows:
        print(
            f"{row.label[:36]:38s} {row.status:10s} {row.caption_kind:8s} "
            f"{row.language:5s} {row.chars:6d}  {','.join(row.tracks[:4]) or '-'}"
        )
        if row.note:
            print(f"{'':38s} └ {row.note}")
        if show_claims:
            for claim in row.claims:
                print(f"{'':38s} · {claim}")
            if row.status == "자막있음" and not row.claims:
                print(f"{'':38s} · (주장을 뽑지 못했다)")


def print_summary(rows: list[Row]) -> None:
    total = len(rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1

    with_caption = [row for row in rows if row.status == "자막있음"]
    manual = sum(1 for row in with_caption if row.caption_kind == "수동")
    auto = len(with_caption) - manual

    print()
    print(f"총 {total}건")
    for status, count in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"  {status}: {count}건 ({count / total * 100:.0f}%)")
    if with_caption:
        print(f"  자막 있는 {len(with_caption)}건 중 수동 {manual}건 / 자동생성 {auto}건")
        avg = sum(row.chars for row in with_caption) / len(with_caption)
        print(f"  평균 글자 수 {avg:,.0f}")
        with_claims = sum(1 for row in with_caption if row.claims)
        if any(row.claims for row in rows):
            print(f"  주장 추출 성공 {with_claims}건 / 실패 {len(with_caption) - with_claims}건")
    if counts.get("차단") or counts.get("미확인"):
        print()
        print("⚠ YouTube가 이 IP를 차단했다. 남은 항목은 확인되지 않았다.")
        print("  다른 네트워크에서 다시 돌리거나 .env에 프록시를 설정해라.")


async def main() -> int:
    parser = argparse.ArgumentParser(description="유튜브 자막 확보 현황을 검증한다.")
    parser.add_argument("targets", nargs="+", help="영상 목록 파일 또는 유튜브 주소/ID")
    parser.add_argument("--claims", action="store_true", help="뽑아낸 주장까지 보여 준다")
    parser.add_argument("--interval", type=float, default=2.0, help="호출 간격(초). 기본 2")
    parser.add_argument("--json", dest="json_path", help="결과를 JSON 파일로 저장한다")
    args = parser.parse_args()

    targets = parse_targets(args)
    if not targets:
        print("확인할 영상이 없다.")
        return 1

    extractor = YouTubeTranscriptExtractor(min_interval=args.interval, cooldown=600)
    api = YouTubeTranscriptApi()
    claim_extractor = RuleBasedClaimExtractor() if args.claims else None

    print(f"{len(targets)}건 확인. 호출 간격 {args.interval}초.")

    rows: list[Row] = []
    for index, (target, label) in enumerate(targets):
        row = await inspect(
            target, label, extractor=extractor, api=api, claim_extractor=claim_extractor
        )
        rows.append(row)
        print(f"  {row.status:10s} {label[:50]}")

        # 차단되면 멈춘다. 계속 돌려도 전부 같은 결과이고, 차단 기간만 길어진다.
        if row.status == "차단":
            for remaining_target, remaining_label in targets[index + 1 :]:
                rows.append(
                    Row(
                        label=remaining_label,
                        video_id=step1_input.classify(remaining_target).video_id,
                        status="미확인",
                        note="앞에서 차단되어 확인하지 않았다",
                    )
                )
            break

    print_table(rows, args.claims)
    print_summary(rows)

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps([asdict(row) for row in rows], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\nJSON 저장: {args.json_path}")

    # 차단이 있었으면 결과가 불완전하다. 자동화에서 구분할 수 있게 종료 코드를 바꾼다.
    return 2 if any(row.status == "차단" for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
