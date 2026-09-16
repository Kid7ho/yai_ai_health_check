#!/usr/bin/env python3
"""식약처 I2710(건강기능식품 품목분류정보)에서 고시형 원료 목록을 만든다.

왜 이 스크립트가 필요한가.

rules/notified_ingredients.yaml은 공전 원문을 대조하지 않은 손 초안이었다.
I2710은 고시형과 개별인정형 기능성 원료의 기능성 문구를 함께 준다(2026-09-17 실측 548건).

고시형과 개별인정형을 가르는 기준
    개별인정형은 원료명 끝에 인정 고시번호가 붙는다. 예: "보이차추출물(제2014-7호)".
    번호가 없는 97건(이름 빈 1건 제외)이 고시형이다. 번호가 붙은 450건은 I-0050(개별인정형 정보) 건수와 같다.

이 스크립트는 판정에 영향을 주는 규칙 파일을 만든다. 그래서 서버가 자동으로
돌리지 않는다. 사람이 실행하고 차이를 확인한 뒤 커밋하고 규칙표 version을 올린다.

    .venv/bin/python scripts/sync_notified.py              # 받고, 차이만 보여 준다
    .venv/bin/python scripts/sync_notified.py --write      # 규칙 파일까지 쓴다
    .venv/bin/python scripts/sync_notified.py --offline    # 받아 둔 사본으로만 만든다

식품안전나라 API는 09시~19시(한국 시간)에 막힌다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402
import yaml  # noqa: E402

from healthcheck.pipeline.step3_claims import load_patterns  # noqa: E402

SERVICE = "I2710"
SNAPSHOT_PATH = ROOT / "data" / "mfds_i2710.json"
NAME_MAP_PATH = ROOT / "rules" / "notified_name_map.yaml"
OUTPUT_PATH = ROOT / "rules" / "notified_ingredients.yaml"

_INDIVIDUAL_NO = re.compile(r"제\s*\d{4}\s*-\s*\d+\s*호")

HEADER = """version: v1
# ════════════════════════════════════════════════════════════════════
# 고시형 기능성원료 목록
#
# scripts/sync_notified.py가 식약처 I2710(건강기능식품 품목분류정보)에서 생성한다.
# 손으로 고치지 말고 rules/notified_name_map.yaml을 고친 뒤 다시 생성한다.
#
# 기능성_내용은 I2710의 PRIMARY_FNCLTY에서 번호·일일섭취량·단서 문구를 걷어낸 것이다.
# 공전 원문(식약처 건강기능식품 종합정보 서비스)과 사람이 대조하기 전까지
# 검수완료는 false이고, 이 데이터로 나온 판정에 경고 로그가 남는다.
# ════════════════════════════════════════════════════════════════════

"""


def is_notified(name: str) -> bool:
    """원료명에 인정 고시번호가 없으면 고시형이다."""
    return bool(name.strip()) and not _INDIVIDUAL_NO.search(name)


def clean_functionality(raw: str | None) -> str:
    """PRIMARY_FNCLTY에서 기능성 문구만 남긴다.

    원문에는 "(1)기능성 내용 : …(2) 일일섭취량 …", "① …", "※ 단서", 영문 병기가 섞여 있다.
    일치도 비교는 효능 대상 단어로 하므로 문구의 뜻을 바꾸지 않는 선에서 걷어낸다.
    """
    text = (raw or "").replace("\r", "")
    # 일일섭취량 이하는 기능성 문구가 아니다
    text = re.split(r"\(\s*\d+\s*\)\s*일일\s*섭취량|일일\s*섭취량\s*:", text)[0]
    lines: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("※") or line.startswith("(영문)"):
            continue
        line = re.sub(r"\(영문\).*$", "", line)
        line = re.sub(r"^\(국문\)\s*", "", line)
        line = re.sub(r"^(\(\s*\d+\s*\)|\(\s*[가-하]\s*\)|[①-⑩]|\d+\))\s*", "", line)
        line = re.sub(r"^기능성\s*내용\s*:\s*", "", line)
        line = re.sub(r"\[[^\]]*\]", "", line)  # [제조방법에 한함] 같은 단서
        line = line.strip(" ,")
        if line:
            lines.append(line)
    return ", ".join(lines)


def derive_targets(functionality: str, effect_targets: tuple[str, ...], synonyms: dict[str, list[str]]) -> list[str]:
    """기능성 문구에서 효능 대상을 뽑는다. 공전 표기가 다르면 같은뜻 표로 잇는다.

    주장은 "혈액순환"이라고 말하고 공전은 "혈행"이라고 쓴다. 일치도 비교는 효능 대상
    단어가 같아야 하므로, 같은뜻 표현이 문구에 있으면 주장 쪽 단어를 함께 넣는다.
    """
    found = [t for t in effect_targets if t in functionality]
    for target, words in synonyms.items():
        if target not in found and any(word in functionality for word in words):
            found.append(target)
    return found


def build_entries(
    rows: list[dict],
    name_map: dict[str, str],
    effect_targets: tuple[str, ...],
    synonyms: dict[str, list[str]] | None = None,
) -> tuple[dict, list[str]]:
    """대응표에 있는 고시형 원료만 규칙 파일 항목으로 만든다. 찾지 못한 대응은 따로 돌려준다."""
    by_name: dict[str, dict] = {}
    for row in rows:
        name = (row.get("PRDCT_NM") or "").strip()
        if is_notified(name):
            by_name[name] = row

    entries: dict[str, dict] = {}
    missing: list[str] = []
    for ingredient, official in name_map.items():
        row = by_name.get(official)
        if row is None:
            missing.append(f"{ingredient} → {official}")
            continue
        functionality = clean_functionality(row.get("PRIMARY_FNCLTY"))
        entries[ingredient] = {
            "기능성_내용": functionality,
            "효능_대상": derive_targets(functionality, effect_targets, synonyms or {}),
            "공식_원료명": official,
            "최종수정일": (row.get("LAST_UPDT_DTM") or "").strip(),
        }
    return entries, missing


def fetch_rows(api_key: str) -> list[dict]:
    rows: list[dict] = []
    start = 1
    while True:
        url = f"http://openapi.foodsafetykorea.go.kr/api/{api_key}/{SERVICE}/json/{start}/{start + 999}"
        body = httpx.get(url, timeout=60).json().get(SERVICE, {})
        code = (body.get("RESULT") or {}).get("CODE")
        if code != "INFO-000":
            raise RuntimeError(f"{SERVICE} 조회 실패: {code} {(body.get('RESULT') or {}).get('MSG')}")
        page = body.get("row") or []
        rows.extend(page)
        total = int(body.get("total_count") or 0)
        start += 1000
        if not page or start > total:
            return rows


def diff(old: dict, new: dict) -> list[str]:
    lines: list[str] = []
    for name in sorted(set(old) | set(new)):
        before, after = old.get(name), new.get(name)
        if before is None:
            lines.append(f"+ {name}: {after['기능성_내용']}")
        elif after is None:
            lines.append(f"- {name}: {before.get('기능성_내용')}")
        elif str(before.get("기능성_내용")) != after["기능성_내용"]:
            lines.append(f"~ {name}\n    이전: {before.get('기능성_내용')}\n    이후: {after['기능성_내용']}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="I2710에서 고시형 원료 목록을 만든다.")
    parser.add_argument("--write", action="store_true", help="rules/notified_ingredients.yaml을 쓴다")
    parser.add_argument("--offline", action="store_true", help="API를 부르지 않고 받아 둔 사본을 쓴다")
    args = parser.parse_args()

    if args.offline:
        rows = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))["rows"]
    else:
        from healthcheck.adapters.mfds.snapshot import in_restricted_hours
        from healthcheck.config import load_settings

        settings = load_settings()
        if not settings.has_mfds_key:
            print("MFDS_API_KEY가 없습니다. .env를 확인하세요.")
            return 1
        if in_restricted_hours():
            print("지금은 식품안전나라 API 제한 시간(09시~19시)입니다. --offline으로 사본을 쓰거나 19시 이후에 실행하세요.")
            return 1
        rows = fetch_rows(settings.mfds_api_key)
        SNAPSHOT_PATH.write_text(
            json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "service": SERVICE, "rows": rows},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    notified_names = [r["PRDCT_NM"].strip() for r in rows if is_notified(r.get("PRDCT_NM") or "")]
    mapping = yaml.safe_load(NAME_MAP_PATH.read_text(encoding="utf-8")) or {}
    name_map = mapping.get("대응") or {}
    entries, missing = build_entries(rows, name_map, load_patterns().effect_targets, mapping.get("효능_같은뜻") or {})

    old = (yaml.safe_load(OUTPUT_PATH.read_text(encoding="utf-8")) or {}).get("원료") or {}
    print(f"I2710 {len(rows)}건 · 고시형 {len(notified_names)}건 · 대응 {len(entries)}종")
    if missing:
        print(f"\n⚠ I2710에서 찾지 못한 대응 {len(missing)}건: {missing}")
    no_targets = [n for n, e in entries.items() if not e["효능_대상"]]
    if no_targets:
        print(f"\n⚠ 효능 대상을 뽑지 못한 원료(일치도 비교 불가): {no_targets}")
    changes = diff(old, entries)
    print(f"\n기존 규칙 파일과 차이 {len(changes)}건")
    for line in changes:
        print(line)
    unmapped = sorted(set(notified_names) - set(name_map.values()))
    print(f"\n대응표에 없는 고시형 원료 {len(unmapped)}건 (판정에 쓰이지 않음)")

    if not args.write:
        print("\n--write 없이 실행해 규칙 파일은 그대로입니다.")
        return 0
    document = {"검수완료": False, "출처": f"식약처 {SERVICE} 건강기능식품 품목분류정보", "원료": entries}
    OUTPUT_PATH.write_text(HEADER + yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=200), encoding="utf-8")
    print(f"\n생성: {OUTPUT_PATH}. 골든셋을 다시 재고 규칙표 version을 올리세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
