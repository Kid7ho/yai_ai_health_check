#!/usr/bin/env python3
"""보존 기간이 지난 개인정보를 정리한다.

하루 한 번 돌리는 것을 전제로 만들었다. 앱 기동 시에도 한 번 돌지만,
서버를 오래 띄워 두면 그것만으로는 부족하다.

행을 지우지 않는다. 만료된 열만 비우고, 사용자 키는 해시로 바꾼다.
누적 확인 건수와 오정보 유형 수는 목표 지표이므로 유지해야 한다.

사용법
    python scripts/purge_expired.py
    python scripts/purge_expired.py --database 다른경로.db
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from healthcheck.config import load_settings  # noqa: E402
from healthcheck.storage.sqlite_repo import SqliteCheckRepository  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="보존 기간이 지난 개인정보를 정리한다.")
    parser.add_argument("--database", help="DB 경로. 기본은 .env의 DATABASE_PATH")
    args = parser.parse_args()

    settings = load_settings()
    path = args.database or settings.database_path
    repository = SqliteCheckRepository(path)

    print(f"저장소: {path}")
    print(f"보존 기간: 원문·자막 {settings.text_retention_days}일 / 사용자 키 {settings.user_key_retention_days}일")

    purged = await repository.purge_expired()
    metrics = await repository.metrics()

    print(f"\n원문·자막 비움 {purged['texts_cleared']}건")
    print(f"사용자 키 해시 처리 {purged['user_keys_hashed']}건")
    print(f"\n누적 확인 건수 {metrics.total_checks}건 / 오정보 유형 {metrics.misinformation_types}종")
    repository.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
