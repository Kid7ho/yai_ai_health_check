"""식약처 개별인정형(I-0040) 사본을 지금 받는다.

식품안전나라 API는 09시~19시(한국 시간)에 막힌다. 그 밖의 시간에 실행한다.
서버가 켜져 있으면 서버가 알아서 받으므로 보통은 실행할 필요가 없다.

    .venv/bin/python scripts/sync_mfds.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from healthcheck.adapters.mfds.foodsafetykorea import FoodSafetyKoreaDataSource  # noqa: E402
from healthcheck.adapters.mfds.snapshot import in_restricted_hours  # noqa: E402
from healthcheck.config import load_settings  # noqa: E402


async def main() -> int:
    settings = load_settings()
    if not settings.has_mfds_key:
        print("MFDS_API_KEY가 없습니다. .env를 확인하세요.")
        return 1
    if in_restricted_hours():
        print("지금은 식품안전나라 API 제한 시간(09시~19시)입니다. 19시 이후에 실행해 주세요.")
        return 1

    source = FoodSafetyKoreaDataSource(
        settings.mfds_api_key, snapshot_path=settings.mfds_snapshot_path
    )
    try:
        updated = await source.refresh_snapshot(force=True)
    finally:
        await source.aclose()

    info = source.snapshot_info() or {}
    if not updated:
        print(f"받지 못했습니다. 이전 사본: {info}")
        return 1
    print(f"받았습니다: {info['rows']}건 → {settings.mfds_snapshot_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
