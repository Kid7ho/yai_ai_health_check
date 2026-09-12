"""공전 표 변환 스크립트 테스트."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "import_notified.py"
spec = importlib.util.spec_from_file_location("import_notified", SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules["import_notified"] = module
spec.loader.exec_module(module)

TABLE = """원료명\t기능성내용
홍삼\t면역력 증진·피로 개선에 도움을 줄 수 있음
홍삼\t기억력 개선에 도움을 줄 수 있음
프로바이오틱스\t유산균 증식 및 유해균 억제, 배변활동 원활에 도움을 줄 수 있음
알 수 없는 원료\t
"""


def test_탭과_쉼표를_모두_읽는다(tmp_path):
    tsv = tmp_path / "a.tsv"
    tsv.write_text(TABLE, encoding="utf-8")
    csv_file = tmp_path / "a.csv"
    csv_file.write_text(TABLE.replace("\t", ","), encoding="utf-8")

    assert len(module.read_rows(tsv)) == 4
    assert len(module.read_rows(csv_file)) == 4


def test_같은_원료의_기능성_문구를_합친다():
    """공전에는 같은 원료가 여러 기능성으로 등재된다."""
    from healthcheck.pipeline.step3_claims import load_patterns

    entries: dict[str, dict] = {}
    for row in module.read_rows(_write(TABLE)):
        name = module.pick(row, module._NAME_KEYS)
        functionality = module.pick(row, module._FUNCTION_KEYS)
        if not name or not functionality:
            continue
        if name in entries:
            entries[name]["기능성_내용"] += f", {functionality}"
        else:
            entries[name] = {"기능성_내용": functionality}

    assert "면역력" in entries["홍삼"]["기능성_내용"]
    assert "기억력" in entries["홍삼"]["기능성_내용"]
    assert load_patterns() is not None


def test_기능성_내용에서_효능_대상을_뽑는다():
    from healthcheck.pipeline.step3_claims import load_patterns

    targets = load_patterns().effect_targets

    assert "면역력" in module.derive_targets("면역력 증진에 도움을 줄 수 있음", targets)
    assert "배변활동" in module.derive_targets(
        "배변활동 원활에 도움을 줄 수 있음", targets
    )
    assert "혈행" in module.derive_targets("혈행 개선에 도움을 줄 수 있음", targets)


def test_원료명이나_기능성이_비면_건너뛴다():
    rows = module.read_rows(_write(TABLE))
    empty = rows[-1]
    assert module.pick(empty, module._FUNCTION_KEYS) == ""


def test_생성된_YAML이_규칙_파일_형식과_맞는다(tmp_path):
    """식약처 어댑터가 읽는 구조와 같아야 한다."""
    output = tmp_path / "notified.yaml"
    sys.argv = ["import_notified.py", str(_write(TABLE)), "--output", str(output)]
    assert module.main() == 0

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert document["검수완료"] is False
    assert "홍삼" in document["원료"]
    assert "기능성_내용" in document["원료"]["홍삼"]
    assert "효능_대상" in document["원료"]["홍삼"]


def test_검수완료가_기본으로_거짓이다(tmp_path):
    """검수 전 데이터로 나온 판정에는 경고가 남아야 한다."""
    output = tmp_path / "notified.yaml"
    sys.argv = ["import_notified.py", str(_write(TABLE)), "--output", str(output)]
    module.main()

    assert yaml.safe_load(output.read_text(encoding="utf-8"))["검수완료"] is False


def _write(text: str) -> Path:
    import tempfile

    path = Path(tempfile.mkdtemp()) / "table.tsv"
    path.write_text(text, encoding="utf-8")
    return path
