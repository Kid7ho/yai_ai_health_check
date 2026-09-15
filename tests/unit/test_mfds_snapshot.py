"""식약처 개별인정형 로컬 사본.

식품안전나라 API가 09시~19시에 "ERROR-503: 09시~19시에는 서비스가 제한됩니다"를
돌려줘 낮 시간 판정이 전부 '알 수 없어요'로 떨어졌다(2026-09-15 실측).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from healthcheck.adapters.mfds.foodsafetykorea import FoodSafetyKoreaDataSource
from healthcheck.adapters.mfds.snapshot import (
    Snapshot,
    SnapshotError,
    download_snapshot,
    in_restricted_hours,
    load_snapshot,
    save_snapshot,
)
from healthcheck.domain.types import Claim

KST = ZoneInfo("Asia/Seoul")
NIGHT = datetime(2026, 9, 15, 21, 0, tzinfo=KST)
DAY = datetime(2026, 9, 15, 10, 0, tzinfo=KST)

ROW_LIVER = {"APLC_RAWMTRL_NM": "흑마늘추출물", "FNCLTY_CN": "간 건강에 도움을 줄 수 있음", "HF_FNCLTY_MTRAL_RCOGN_NO": "2016-16"}
ROW_BP = {"APLC_RAWMTRL_NM": "마늘추출물", "FNCLTY_CN": "높은 혈압 개선에 도움을 줄 수 있음", "HF_FNCLTY_MTRAL_RCOGN_NO": "2018-3"}
ROW_OTHER = {"APLC_RAWMTRL_NM": "밀크씨슬추출물", "FNCLTY_CN": "간 건강에 도움을 줄 수 있음", "HF_FNCLTY_MTRAL_RCOGN_NO": "2004-1"}
GARLIC_BP = Claim(ingredient="마늘", claimed_effect="혈압을 낮추는 데 좋다고", intensity="SUGGESTIVE", span=(0, 2))


def rows(n: int) -> list[dict]:
    return [{"APLC_RAWMTRL_NM": f"원료{i}", "FNCLTY_CN": "간 건강에 도움을 줄 수 있음"} for i in range(n)]


def paged_handler(all_rows: list[dict], calls: list[str], *, code: str = "INFO-000", total: int | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if code != "INFO-000":
            body = {"I-0040": {"RESULT": {"CODE": code, "MSG": "09시~19시에는 서비스가 제한됩니다."}}}
            return httpx.Response(200, json=body)
        start, end = map(int, re.search(r"/json/(\d+)/(\d+)", str(request.url)).groups())
        page = all_rows[start - 1 : end]
        body = {"I-0040": {"total_count": str(total if total is not None else len(all_rows)), "row": page, "RESULT": {"CODE": "INFO-000", "MSG": "정상"}}}
        return httpx.Response(200, json=body)

    return handler


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("hour", "minute", "restricted"),
    [(8, 59, False), (9, 0, True), (18, 59, True), (19, 0, False), (2, 0, False)],
)
def test_제한_시간은_한국_시간_09시부터_19시_전까지다(hour, minute, restricted):
    now = datetime(2026, 9, 15, hour, minute, tzinfo=KST).astimezone(timezone.utc)
    assert in_restricted_hours(now) is restricted


async def test_전체_인정_건을_쪽_단위로_받는다():
    calls: list[str] = []
    snapshot = await download_snapshot(
        client_for(paged_handler(rows(250), calls)), api_key="k", base_url="http://api", now=NIGHT
    )

    assert len(snapshot.rows) == 250
    assert [re.search(r"/json/(\d+/\d+)", c).group(1) for c in calls] == ["1/100", "101/200", "201/300"]


async def test_받은_건수가_전체와_다르면_버린다():
    with pytest.raises(SnapshotError):
        await download_snapshot(
            client_for(paged_handler(rows(150), [], total=200)), api_key="k", base_url="http://api"
        )


async def test_제한_시간_오류를_받으면_실패로_본다():
    with pytest.raises(SnapshotError, match="ERROR-503"):
        await download_snapshot(
            client_for(paged_handler([], [], code="ERROR-503")), api_key="k", base_url="http://api"
        )


def test_저장한_사본을_다시_읽는다(tmp_path):
    path = tmp_path / "data" / "mfds.json"
    save_snapshot(path, Snapshot(fetched_at=NIGHT, rows=(ROW_LIVER, ROW_BP)))

    loaded = load_snapshot(path)

    assert loaded is not None
    assert loaded.rows == (ROW_LIVER, ROW_BP)
    assert loaded.fetched_at == NIGHT


def test_깨진_사본은_쓰지_않는다(tmp_path):
    path = tmp_path / "mfds.json"
    path.write_text(json.dumps({"format": 1, "fetched_at": NIGHT.isoformat(), "total_count": 5, "rows": [ROW_BP]}), encoding="utf-8")
    assert load_snapshot(path) is None
    path.write_text("{not json", encoding="utf-8")
    assert load_snapshot(path) is None


def test_부분_검색은_API처럼_신청원료명_포함으로_본다():
    snapshot = Snapshot(fetched_at=NIGHT, rows=(ROW_LIVER, ROW_BP, ROW_OTHER))
    assert snapshot.search("마늘") == [ROW_LIVER, ROW_BP]


async def test_사본이_있으면_API를_부르지_않고_판정한다(tmp_path):
    path = tmp_path / "mfds.json"
    save_snapshot(path, Snapshot(fetched_at=datetime.now(timezone.utc), rows=(ROW_LIVER, ROW_BP)))
    calls: list[str] = []
    source = FoodSafetyKoreaDataSource(
        "k", client=client_for(paged_handler([], calls, code="ERROR-503")), snapshot_path=path
    )

    evidence = await source.lookup(GARLIC_BP)

    assert calls == []
    assert evidence.recognition == "INDIVIDUAL"
    assert evidence.recognition_no == "2018-3"


async def test_사본이_없으면_API를_직접_부른다(tmp_path):
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        body = {"I-0040": {"total_count": "1", "row": [ROW_BP], "RESULT": {"CODE": "INFO-000"}}}
        return httpx.Response(200, json=body)

    source = FoodSafetyKoreaDataSource("k", client=client_for(handler), snapshot_path=tmp_path / "none.json")

    evidence = await source.lookup(GARLIC_BP)

    assert len(calls) == 1 and "APLC_RAWMTRL_NM=" in calls[0]
    assert evidence.recognition == "INDIVIDUAL"


async def test_제한_시간에는_사본을_받지_않는다(tmp_path):
    calls: list[str] = []
    source = FoodSafetyKoreaDataSource("k", client=client_for(paged_handler(rows(3), calls)), snapshot_path=tmp_path / "m.json")

    assert await source.refresh_snapshot(now=DAY.astimezone(timezone.utc)) is False
    assert calls == []

    assert await source.refresh_snapshot(now=NIGHT.astimezone(timezone.utc)) is True
    assert source.snapshot_info()["rows"] == 3


async def test_하루가_지나지_않은_사본은_다시_받지_않는다(tmp_path):
    path = tmp_path / "m.json"
    fetched = NIGHT.astimezone(timezone.utc)
    save_snapshot(path, Snapshot(fetched_at=fetched, rows=(ROW_BP,)))
    calls: list[str] = []
    source = FoodSafetyKoreaDataSource("k", client=client_for(paged_handler(rows(3), calls)), snapshot_path=path)

    assert await source.refresh_snapshot(now=fetched + timedelta(hours=5)) is False
    assert calls == []
    assert await source.refresh_snapshot(now=fetched + timedelta(hours=24, minutes=5)) is True


async def test_받기에_실패하면_이전_사본을_그대로_쓴다(tmp_path):
    path = tmp_path / "m.json"
    old = NIGHT.astimezone(timezone.utc) - timedelta(days=2)
    save_snapshot(path, Snapshot(fetched_at=old, rows=(ROW_BP,)))
    source = FoodSafetyKoreaDataSource("k", client=client_for(paged_handler([], [], code="ERROR-503")), snapshot_path=path)

    assert await source.refresh_snapshot(now=NIGHT.astimezone(timezone.utc)) is False
    assert source.snapshot_info()["rows"] == 1
    assert load_snapshot(path).rows == (ROW_BP,)
    assert (await source.lookup(GARLIC_BP)).recognition == "INDIVIDUAL"
