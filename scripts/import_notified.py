#!/usr/bin/env python3
"""건강기능식품 공전의 고시형 기능성원료 표를 규칙 파일로 바꾼다.

왜 이 스크립트가 필요한가.

고시형 기능성원료의 기능성 내용은 공개 API로 제공되지 않는다. 확인한 결과는
이렇다.

  · 식약처 오픈API I-0040(773건)과 I-0050(447건)은 모두 개별인정형이다.
    둘 다 인정번호가 있고 고시형은 인정번호가 없다.
  · 법제처 API로 고시 「건강기능식품의 기준 및 규격」을 받으면 조문 내용이
    "자세한 내용은 상단 메뉴 버튼을 이용하십시오"로만 나온다. 실제 표는
    첨부파일(한글·PDF)이다.
  · C002(품목제조신고)에는 기능성 문구가 없다.

그래서 사람이 공전 첨부파일에서 표를 뽑아 주어야 한다. 이 스크립트는 그
표를 받아 rules/notified_ingredients.yaml로 바꾼다. 손으로 YAML을 쓰면
오타가 판정에 바로 영향을 주기 때문이다.

입력 형식
    탭 또는 쉼표로 구분한 표. 첫 줄은 머리글.
    필요한 열은 둘이다.
        원료명        (원료명, 기능성원료명, 명칭 중 아무 이름)
        기능성내용    (기능성내용, 기능성 내용, 기능성)
    효능 대상은 기능성 내용에서 자동으로 뽑는다.

사용법
    python scripts/import_notified.py 공전표.tsv
    python scripts/import_notified.py 공전표.tsv --dry-run
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402

from healthcheck.pipeline.step3_claims import load_patterns  # noqa: E402

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "rules" / "notified_ingredients.yaml"

_NAME_KEYS = ("원료명", "기능성원료명", "기능성 원료명", "명칭", "원료")
_FUNCTION_KEYS = ("기능성내용", "기능성 내용", "기능성", "기능성 문구")

HEADER = """version: v0
# ════════════════════════════════════════════════════════════════════
# 고시형 기능성원료 목록
#
# 식약처 오픈API I-0040은 개별인정형 원료만 담습니다. 홍삼, 프로바이오틱스
# 같은 고시형 원료는 건강기능식품 공전이 출처이며 API로 제공되지 않습니다.
#
# 이 파일은 scripts/import_notified.py가 공전 표에서 생성합니다.
# 손으로 고치지 말고 원본 표를 고친 뒤 다시 생성하십시오.
#
# 검수완료를 true로 바꾸기 전까지는 이 데이터로 나온 판정에 경고 로그가
# 남습니다. 기능성 내용이 공전 원문과 일치하는지 의료 자문의가 확인한 뒤
# 바꾸십시오.
# ════════════════════════════════════════════════════════════════════

"""


def read_rows(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    delimiter = "\t" if text.count("\t") > text.count(",") else ","
    return list(csv.DictReader(text.splitlines(), delimiter=delimiter))


def pick(row: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in row:
        if key and key.strip() in keys:
            return (row[key] or "").strip()
    return ""


def derive_targets(functionality: str, targets: tuple[str, ...]) -> list[str]:
    """기능성 내용에서 효능 대상을 뽑는다.

    판정의 일치도 비교에 쓰는 값이다. 사람이 따로 적지 않아도 되도록
    rules/claim_patterns.yaml의 효능 대상 목록과 맞춰 본다.
    """
    return [target for target in targets if target in functionality]


def main() -> int:
    parser = argparse.ArgumentParser(description="공전 표를 고시형 원료 규칙 파일로 바꾼다.")
    parser.add_argument("table", help="공전에서 뽑은 표 (TSV 또는 CSV)")
    parser.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 결과만 보여 준다")
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    args = parser.parse_args()

    rows = read_rows(Path(args.table))
    if not rows:
        print("표가 비어 있습니다.")
        return 1

    effect_targets = load_patterns().effect_targets

    entries: dict[str, dict] = {}
    skipped: list[str] = []
    no_targets: list[str] = []

    for row in rows:
        name = pick(row, _NAME_KEYS)
        functionality = pick(row, _FUNCTION_KEYS)

        if not name or not functionality:
            skipped.append(name or "(이름 없음)")
            continue

        # 공전에는 같은 원료가 여러 기능성으로 등재된다. 문구를 합친다.
        if name in entries:
            existing = entries[name]["기능성_내용"]
            if functionality not in existing:
                entries[name]["기능성_내용"] = f"{existing}, {functionality}"
        else:
            entries[name] = {"기능성_내용": functionality, "효능_대상": []}

    for name, entry in entries.items():
        entry["효능_대상"] = derive_targets(entry["기능성_내용"], effect_targets)
        if not entry["효능_대상"]:
            no_targets.append(name)

    document = {"검수완료": False, "원료": entries}
    body = HEADER + yaml.safe_dump(
        document, allow_unicode=True, sort_keys=False, width=200
    )

    print(f"원료 {len(entries)}종")
    if skipped:
        print(f"건너뜀 {len(skipped)}건 (원료명 또는 기능성 내용 없음): {skipped[:5]}")
    if no_targets:
        print(f"\n⚠ 효능 대상을 뽑지 못한 원료 {len(no_targets)}종")
        print("  claim_patterns.yaml의 효능_대상 목록에 없는 표현입니다.")
        print("  이 원료는 일치도 비교가 되지 않아 판정이 보류됩니다.")
        for name in no_targets[:10]:
            print(f"    {name}: {entries[name]['기능성_내용'][:60]}")

    if args.dry_run:
        print("\n--dry-run 이므로 파일을 쓰지 않았습니다.")
        return 0

    Path(args.output).write_text(body, encoding="utf-8")
    print(f"\n생성: {args.output}")
    print("검수 후 파일의 검수완료를 true로 바꾸십시오.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
