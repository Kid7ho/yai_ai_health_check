"""식약처 데이터 소스 실제 구현 (식품안전나라 오픈API).

확인된 스펙 — sample 키로 실제 호출해 검증했다.
  GET http://openapi.foodsafetykorea.go.kr/api/{인증키}/I-0040/json/{시작}/{끝}
      뒤에 /APLC_RAWMTRL_NM={값} 을 붙이면 신청원료명 부분 검색이 된다
  응답 {"I-0040": {"total_count": "773", "row": [...], "RESULT": {"CODE": ...}}}
  행 컬럼 HF_FNCLTY_MTRAL_RCOGN_NO, PRMS_DT, BSSH_NM, INDUTY_NM, ADDR,
          APLC_RAWMTRL_NM, FNCLTY_CN, DAY_INTK_CN, IFTKN_ATNT_MATR_CN
  RESULT.CODE INFO-000 정상 / INFO-200 데이터 없음 / INFO-100 인증키 무효

낮 시간(09시~19시) API 제한 때문에 개별인정형은 로컬 사본(snapshot.py)에서
먼저 찾는다. 사본이 없을 때만 API를 직접 부른다.

I-0040은 개별인정형 원료만 담는다. 고시형 원료(홍삼, 프로바이오틱스 등)는
건강기능식품 공전이 출처이고 API로 제공되지 않으므로
rules/notified_ingredients.yaml을 먼저 조회한다. 그 파일은 아직 검수 전
초안이므로 사용할 때마다 경고 로그를 남긴다.

부당광고 적발 정보는 전용 API를 특정하지 못했다. 데이터를 지어내지 않고
ad_violation_hit을 항상 False로 둔다. 그래서 지금은 R-020과 R-030이
실제로는 발동하지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml

from healthcheck.adapters.mfds.snapshot import (
    Snapshot,
    SnapshotError,
    download_snapshot,
    in_restricted_hours,
    load_snapshot,
    save_snapshot,
)
from healthcheck.domain.types import Claim, MatchLevel, MfdsEvidence

logger = logging.getLogger(__name__)

BASE_URL = "http://openapi.foodsafetykorea.go.kr/api"
SERVICE_ID = "I-0040"

RULES_DIR = Path(__file__).resolve().parents[4] / "rules"
DEFAULT_NOTIFIED_PATH = RULES_DIR / "notified_ingredients.yaml"
DEFAULT_PATTERNS_PATH = RULES_DIR / "claim_patterns.yaml"

DEFAULT_TIMEOUT = 8.0
# 한 성분에 인정 건이 여러 개다. 기능성 문구가 조금씩 달라서 전부 받아
# 주장과 가장 잘 맞는 것을 고른다. 10건만 받았을 때 프로바이오틱스(37건),
# 가르시니아(19건), 밀크씨슬(18건) 등은 뒤쪽의 맞는 인정 건을 놓쳤다.
MAX_ROWS = 100

# 같은 성분·효능 주장의 조회 결과를 캐시하는 시간(초). 인정 현황은 자주 바뀌지 않는다.
CACHE_TTL = 24 * 3600

# 신청원료명에 이 말이 있으면 여러 원료를 섞은 복합물이다.
_COMPOUND_MARKERS = ("복합물", "혼합", "복합추출물")


class MfdsLookupError(RuntimeError):
    """식약처 조회 실패. ④ 검증 단계가 잡아 lookup_failed로 바꾼다."""


@lru_cache(maxsize=2)
def _load_notified(path: Path | str = DEFAULT_NOTIFIED_PATH) -> tuple[dict, bool]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return raw.get("원료") or {}, bool(raw.get("검수완료"))


@lru_cache(maxsize=2)
def _load_targets(path: Path | str = DEFAULT_PATTERNS_PATH) -> tuple[str, ...]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    targets: list[str] = []
    for group in (raw.get("효능_대상") or {}).values():
        targets.extend(group or ())
    # 긴 표기를 먼저 본다.
    return tuple(sorted(set(targets), key=len, reverse=True))


def targets_in(text: str) -> list[str]:
    """문구에 들어 있는 효능 대상."""
    return [target for target in _load_targets() if target in text]


def match_level(claim: Claim, functionality_text: str, covered: list[str]) -> MatchLevel:
    """주장 효능이 인정 기능성 문구와 얼마나 맞는가.

    주장에서 찾은 효능 대상이 인정 문구에도 있으면 일치로 본다.
    단정 표현은 완전 일치로 보지 않는다. 인정 문구는 모두 '도움을 줄 수 있음'
    이라는 완화 표현이므로, '낫는다'는 인정 범위를 넘는 주장이다.
    """
    claimed = targets_in(claim.claimed_effect)
    if not claimed:
        # 효능 대상을 특정하지 못했다. 인정 문구와 비교할 수 없다.
        return "PARTIAL"

    overlap = [target for target in claimed if target in functionality_text or target in covered]
    if not overlap:
        return "MISMATCH"
    if claim.intensity == "ASSERTIVE":
        return "PARTIAL"
    return "EXACT" if len(overlap) == len(claimed) else "PARTIAL"


class FoodSafetyKoreaDataSource:
    """고시형 목록을 먼저 보고, 없으면 I-0040을 호출한다."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
        notified_path: Path | str = DEFAULT_NOTIFIED_PATH,
        snapshot_path: Path | str | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("MFDS_API_KEY가 없습니다. .env를 확인하세요.")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._notified_path = notified_path
        self.call_count = 0
        self._cache: dict[tuple[str, str, str], tuple[float, MfdsEvidence]] = {}
        self.cache_hits = 0
        # 식품안전나라는 같은 인증키로 동시에 들어온 요청을 "INFO-500: 현재 접속 중인
        # 인증키입니다"로 거절한다(2026-09-15 실측, 한 기사에서 주장 2건을 동시에 조회).
        # API 호출은 한 번에 하나씩 보내고, 같은 성분의 인정 건은 한 번만 받는다.
        self._api_lock = asyncio.Lock()
        self._rows_cache: dict[str, tuple[float, list[dict]]] = {}
        self._snapshot_path = Path(snapshot_path) if snapshot_path else None
        self._snapshot: Snapshot | None = (
            load_snapshot(self._snapshot_path) if self._snapshot_path else None
        )
        if self._snapshot is not None:
            logger.info(
                "식약처 개별인정형 사본 사용: %d건, 받은 시각 %s",
                len(self._snapshot.rows),
                self._snapshot.fetched_at.isoformat(timespec="minutes"),
            )
        elif self._snapshot_path is not None:
            logger.warning(
                "식약처 사본이 없다. 사본을 받기 전까지 API를 직접 부르며 09시~19시에는 실패한다."
            )

    async def lookup(self, claim: Claim) -> MfdsEvidence:
        key = (claim.ingredient, claim.claimed_effect, claim.intensity)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_TTL:
            self.cache_hits += 1
            return cached[1]

        notified = self._lookup_notified(claim)
        evidence = notified if notified is not None else await self._lookup_individual(claim)

        # 조회에 성공한 결과만 캐시한다. 실패는 예외로 올라가 여기까지 오지 않는다.
        self._cache[key] = (time.monotonic(), evidence)
        return evidence

    # ── 고시형 (공전 기반 로컬 목록) ──────────────────────────────

    def _lookup_notified(self, claim: Claim) -> MfdsEvidence | None:
        table, reviewed = _load_notified(self._notified_path)
        entry = table.get(claim.ingredient)
        if not entry:
            return None

        functionality = str(entry.get("기능성_내용") or "").strip()
        if not functionality:
            # 문구가 비어 있으면 고시형으로 단정할 수 없다. API 조회로 넘긴다.
            return None

        if not reviewed:
            logger.warning(
                "고시형 목록이 검수 전 초안이다. 성분=%s 판정에 초안 데이터가 쓰였다.",
                claim.ingredient,
            )

        covered = [str(target) for target in entry.get("효능_대상") or ()]
        level = match_level(claim, functionality, covered)

        if level == "MISMATCH":
            return _not_recognized_for_this_effect(claim, [functionality])

        return MfdsEvidence(
            recognition="NOTIFIED",
            functionality_text=functionality,
            match_level=level,
            ad_violation_hit=False,
        )

    # ── 개별인정형 (오픈API I-0040) ──────────────────────────────

    async def _lookup_individual(self, claim: Claim) -> MfdsEvidence:
        # TODO(검수필요): APLC_RAWMTRL_NM 부분 검색은 다른 제품 형태의 인정
        # 사실을 끌어온다. 실제 호출에서 "마늘"이 "흑마늘추출물"(간 건강 인정)에
        # 걸려, 마늘·혈압 주장이 INDIVIDUAL/MISMATCH로 판정되어 R-060 WARNING이
        # 나왔다. 인정 여부는 성분 하나가 아니라 (성분, 효능) 쌍에 대한 것이므로,
        # 어떤 인정 건도 주장 효능을 다루지 않으면 그 효능에 대해서는 미인정으로
        # 보는 편이 정확하다. 판정 결과가 바뀌는 변경이라 자문의 검수 후 적용한다.
        if self._snapshot is not None:
            rows = self._snapshot.search(claim.ingredient)
        else:
            rows = await self._fetch_rows(claim.ingredient)
        if not rows:
            return MfdsEvidence(
                recognition="NOT_RECOGNIZED",
                functionality_text=None,
                match_level="NOT_APPLICABLE",
            )

        best = self._best_row(claim, rows)
        functionality = _clean(best.get("FNCLTY_CN"))
        level = match_level(claim, functionality or "", [])

        # 복합물의 인정은 여러 원료를 섞은 제품 기준이다. 단일 성분 주장과 완전히
        # 일치한다고 보지 않는다. "포도블루베리추출혼합분말"의 인정이 블루베리
        # 단일 성분의 인정처럼 쓰였다.
        if level == "EXACT" and _is_compound(best.get("APLC_RAWMTRL_NM")):
            level = "PARTIAL"

        if level == "MISMATCH":
            # 인정 여부는 성분 하나가 아니라 (성분, 효능) 쌍에 대한 것이다.
            # "마늘"이 간 건강으로 인정됐다는 사실은 혈압 주장의 근거가 아니다.
            # 신청원료명 부분 검색이 다른 제품 형태의 인정까지 끌어오므로,
            # 주장 효능을 다루는 인정 건이 없으면 그 효능에 대해 미인정으로 본다.
            others = [text for text in (_clean(row.get("FNCLTY_CN")) for row in rows) if text]
            return _not_recognized_for_this_effect(claim, others)

        return MfdsEvidence(
            recognition="INDIVIDUAL",
            functionality_text=functionality,
            match_level=level,
            ad_violation_hit=False,
            recognition_no=_clean(best.get("HF_FNCLTY_MTRAL_RCOGN_NO")),
        )

    def _best_row(self, claim: Claim, rows: list[dict]) -> dict:
        """한 성분에 인정 건이 여러 개다. 주장과 가장 잘 맞는 문구를 고른다.

        MISMATCH만 나오는 상황에서 임의의 한 건을 고르면 판정이 우연에 좌우된다.
        그래서 일치도가 높은 건을 우선한다.
        """
        priority = {"EXACT": 0, "PARTIAL": 1, "MISMATCH": 2, "NOT_APPLICABLE": 3}
        return min(
            rows,
            key=lambda row: (
                priority[match_level(claim, _clean(row.get("FNCLTY_CN")) or "", [])],
                # 같은 일치도면 복합물이 아닌 단일 원료 인정을 먼저 쓴다.
                _is_compound(row.get("APLC_RAWMTRL_NM")),
                len(str(row.get("APLC_RAWMTRL_NM") or "")),
            ),
        )

    async def _fetch_rows(self, ingredient: str) -> list[dict]:
        async with self._api_lock:
            cached = self._rows_cache.get(ingredient)
            if cached and time.monotonic() - cached[0] < CACHE_TTL:
                return cached[1]
            try:
                rows = await self._fetch_rows_once(ingredient)
            except MfdsLookupError as error:
                # 다른 프로세스나 사본 받기와 겹쳐 거절됐을 수 있다. 한 번만 다시 시도한다.
                if "INFO-500" not in str(error):
                    raise
                await asyncio.sleep(1.0)
                rows = await self._fetch_rows_once(ingredient)
            self._rows_cache[ingredient] = (time.monotonic(), rows)
            return rows

    async def _fetch_rows_once(self, ingredient: str) -> list[dict]:
        url = (
            f"{self._base_url}/{self._api_key}/{SERVICE_ID}/json/1/{MAX_ROWS}"
            f"/APLC_RAWMTRL_NM={quote(ingredient)}"
        )
        client = self._ensure_client()

        try:
            response = await client.get(url, timeout=self._timeout)
        except httpx.HTTPError as error:
            raise MfdsLookupError(f"식약처 통신 오류: {error}") from error

        if response.status_code != 200:
            raise MfdsLookupError(f"식약처 응답 {response.status_code}")

        try:
            payload = response.json()[SERVICE_ID]
        except (ValueError, KeyError, TypeError) as error:
            raise MfdsLookupError("식약처 응답 형식이 예상과 다르다") from error

        code = (payload.get("RESULT") or {}).get("CODE", "")
        if code == "INFO-200":
            # 해당하는 데이터가 없다. 오류가 아니다.
            return []
        if code and code != "INFO-000":
            message = (payload.get("RESULT") or {}).get("MSG", "")
            raise MfdsLookupError(f"식약처 오류 {code}: {message}")

        rows = payload.get("row") or []
        return [row for row in rows if isinstance(row, dict)]

    # ── 로컬 사본 ────────────────────────────────────────────────

    def snapshot_info(self) -> dict | None:
        """상태 확인용 요약. 사본을 쓰지 않는 설정이면 None이다."""
        if self._snapshot_path is None:
            return None
        if self._snapshot is None:
            return {"rows": 0, "fetched_at": None, "stale": True}
        return {
            "rows": len(self._snapshot.rows),
            "fetched_at": self._snapshot.fetched_at.isoformat(timespec="seconds"),
            "stale": self._snapshot.is_stale(),
        }

    async def refresh_snapshot(self, *, force: bool = False, now=None) -> bool:
        """사본이 없거나 하루가 지났으면 새로 받는다. 받았으면 True다.

        제한 시간에는 부르지 않는다. 불러도 ERROR-503만 받는다.
        실패하면 이전 사본을 그대로 쓴다.
        """
        if self._snapshot_path is None:
            return False
        if not force:
            if self._snapshot is not None and not self._snapshot.is_stale(now):
                return False
            if in_restricted_hours(now):
                return False

        try:
            async with self._api_lock:
                snapshot = await download_snapshot(
                    self._ensure_client(),
                    api_key=self._api_key,
                    base_url=self._base_url,
                    service_id=SERVICE_ID,
                    now=now,
                )
            save_snapshot(self._snapshot_path, snapshot)
        except (SnapshotError, OSError) as error:
            logger.warning("식약처 사본을 새로 받지 못해 이전 사본을 쓴다: %s", error)
            return False

        self._snapshot = snapshot
        # 인정 현황이 바뀌었을 수 있으므로 조회 캐시를 비운다.
        self._cache.clear()
        logger.info("식약처 사본 갱신: %d건", len(snapshot.rows))
        return True

    async def run_snapshot_refresh(self, interval_seconds: float = 1800) -> None:
        """서버가 켜져 있는 동안 주기적으로 사본을 확인한다. 앱 수명주기가 취소한다."""
        while True:
            try:
                await self.refresh_snapshot()
            except Exception:
                logger.exception("식약처 사본 확인 중 오류")
            await asyncio.sleep(interval_seconds)

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owns_client = True
        self.call_count += 1
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


def _not_recognized_for_this_effect(claim: Claim, others: list[str]) -> MfdsEvidence:
    """주장 효능에 대해서는 인정된 기능성이 없다.

    성분이 다른 효능으로 인정받은 사실은 other_recognitions에 남긴다. 판정에는
    쓰지 않지만, 어르신에게 왜 그런지 설명할 때 쓰고 역추적에도 필요하다.
    """
    logger.info(
        "성분은 인정 원료이나 주장 효능(%s)에 대한 인정은 없다: %s",
        claim.claimed_effect[:20],
        claim.ingredient,
    )
    return MfdsEvidence(
        recognition="NOT_RECOGNIZED",
        functionality_text=None,
        match_level="NOT_APPLICABLE",
        ad_violation_hit=False,
        other_recognitions=tuple(dict.fromkeys(others))[:3],
    )


def _is_compound(name: object) -> bool:
    return any(marker in str(name or "") for marker in _COMPOUND_MARKERS)


def _clean(value: object) -> str | None:
    """식약처 데이터에는 따옴표와 등급 표기가 섞여 있다. 앞뒤만 정리한다."""
    if value is None:
        return None
    text = str(value).strip().strip('"').strip("“”").strip()
    return text or None
