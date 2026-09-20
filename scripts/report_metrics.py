#!/usr/bin/env python3
"""판정 기록을 성과 공유회용 요약으로 뽑는다.

운영 서버에서 돌린다(판정 DB는 볼륨 /data/healthcheck.db에 있다).

    fly ssh console --app yai-health-check -C "python /app/scripts/report_metrics.py"
    python3 scripts/report_metrics.py --db data/healthcheck.db --days 30

개인을 식별하는 값은 뽑지 않는다. 건수와 비율만 센다.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from datetime import date, timedelta

LABEL_KO = {
    "TRUSTWORTHY": "믿을 만해요",
    "CAUTION": "조심하세요",
    "WARNING": "주의하세요",
    "UNKNOWN": "알 수 없어요",
}
KIND_KO = {"YOUTUBE": "유튜브 영상", "OTHER_LINK": "기사·블로그", "PLAIN_TEXT": "문장", "YOUTUBE_OTHER": "유튜브(영상 아님)"}
# 확인이 아니라 안내로 끝난 판정. 규칙 ID가 INPUT-으로 시작한다.
NOTICE_KO = {
    "INPUT-NOT_TARGET": "확인 대상 아님",
    "INPUT-NO_CLAIM": "주장 없음",
    "INPUT-LOGIN_REQUIRED": "로그인 필요",
    "INPUT-YOUTUBE_NOT_VIDEO": "영상 링크 아님",
}


def bar(count: int, total: int, width: int = 24) -> str:
    filled = round(width * count / total) if total else 0
    return "█" * filled + "·" * (width - filled)


def main() -> int:
    parser = argparse.ArgumentParser(description="판정 기록 요약")
    parser.add_argument("--db", default="/data/healthcheck.db")
    parser.add_argument("--days", type=int, default=30, help="날짜별 표에 보여 줄 기간")
    args = parser.parse_args()

    rows = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True).execute(
        "SELECT substr(created_at, 1, 10), label, rule_id, input_kind, ingredient, from_cache, user_key FROM checks"
    ).fetchall()
    if not rows:
        print("기록이 없습니다:", args.db)
        return 0

    total = len(rows)
    days = sorted({row[0] for row in rows})
    print(f"■ 누적 확인 {total}건 · {days[0]} ~ {days[-1]}")
    print(f"  서로 다른 사용자 {len({row[6] for row in rows if row[6]})}명 (사용자 키는 30일 뒤 지워진다)")
    print(f"  저장된 결과로 바로 답한 건수 {sum(1 for row in rows if row[5])}건")

    print("\n■ 신호등")
    for label, count in Counter(row[1] for row in rows).most_common():
        print(f"  {LABEL_KO.get(label, label):<8} {bar(count, total)} {count:>4}건 {count / total:>5.0%}")

    print("\n■ 무엇을 보냈나")
    for kind, count in Counter(row[3] for row in rows).most_common():
        print(f"  {KIND_KO.get(kind, kind):<16} {count:>4}건 {count / total:>5.0%}")

    notices = Counter(row[2] for row in rows if str(row[2]).startswith("INPUT-"))
    if notices:
        print("\n■ 확인하지 못하고 안내로 끝난 건")
        for rule, count in notices.most_common():
            print(f"  {NOTICE_KO.get(rule, rule):<16} {count:>4}건 {count / total:>5.0%}")

    print("\n■ 많이 확인한 성분")
    for ingredient, count in Counter(row[4] for row in rows if row[4]).most_common(10):
        print(f"  {ingredient:<12} {count:>4}건")

    print(f"\n■ 최근 {args.days}일")
    counts = Counter(row[0] for row in rows)
    peak = max(counts.values())
    today = date.fromisoformat(days[-1])
    for offset in range(args.days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        count = counts.get(day, 0)
        if count or day >= days[0]:
            print(f"  {day} {bar(count, peak, 20)} {count:>4}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
