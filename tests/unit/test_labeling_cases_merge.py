"""분류 시트 사례를 다시 만들어도 이미 내보낸 사례와 번호가 바뀌지 않는지 확인한다."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_labeling_cases.py"
spec = importlib.util.spec_from_file_location("build_labeling_cases", SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules["build_labeling_cases"] = module
spec.loader.exec_module(module)


def case(case_id: str, text: str, source: str = "후보 문장", statement: str = "") -> dict:
    return {"id": case_id, "source": source, "input": text, "claims": [{"statement": statement or text}]}


def test_기존_사례는_번호와_내용을_유지하고_새_사례만_뒤에_붙인다(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "DIR", tmp_path)
    published = [case("lab-001", "홍삼 면역"), case("lab-002", "마늘 혈압"), case("link-01", "https://a", "실제 링크")]
    (tmp_path / "cases.json").write_text(json.dumps(published, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "cells.json").write_text(json.dumps([{"id": c["id"]} for c in published]), encoding="utf-8")

    # 다시 만든 결과: 선정 순서가 바뀌고, 추출이 달라지고, 사례 하나가 빠지고, 새 사례가 생겼다
    rebuilt = [
        case("lab-001", "마늘 혈압", statement="바뀐 추출"),
        case("lab-002", "루테인 눈"),
        case("lab-003", "홍삼 면역", source="조회 실패 가정"),
        case("link-01", "https://b", "실제 링크"),
    ]
    cells = [{"id": c["id"], "row": "x"} for c in rebuilt]
    merged, merged_cells, added = module.merge_published(rebuilt, cells)

    by_id = {c["id"]: c for c in merged}
    assert added == 3
    assert by_id["lab-002"]["input"] == "마늘 혈압" and by_id["lab-002"]["claims"][0]["statement"] == "마늘 혈압"
    assert by_id["lab-001"]["input"] == "홍삼 면역"  # 이번 선정에서 빠져도 지우지 않는다
    assert by_id["lab-003"]["input"] == "루테인 눈"
    assert by_id["lab-004"]["source"] == "조회 실패 가정"
    assert by_id["link-02"]["input"] == "https://b"
    assert [c["id"] for c in merged_cells] == ["lab-001", "lab-002", "link-01", "lab-003", "lab-004", "link-02"]
