"""I2710 고시형 원료 목록 생성 스크립트 테스트."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sync_notified.py"
spec = importlib.util.spec_from_file_location("sync_notified", SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules["sync_notified"] = module
spec.loader.exec_module(module)


def test_인정_고시번호가_붙은_원료는_개별인정형이다():
    assert module.is_notified("마늘")
    assert module.is_notified("EPA 및 DHA 함유 유지")
    assert not module.is_notified("보이차추출물(제2014-7호)")
    assert not module.is_notified("PMO 참밀알부민(New제2007-23호)")
    assert not module.is_notified("  ")


def test_기능성_문구에서_번호와_일일섭취량을_걷어낸다():
    raw = (
        "(1)기능성 내용 : 면역력 증진･피로개선에 도움을 줄 수 있음\n"
        "(2) 일일섭취량\n (가)면역력 증진에 도움을 줄 수 있음 : 진세노사이드 3~80 mg"
    )
    assert module.clean_functionality(raw) == "면역력 증진･피로개선에 도움을 줄 수 있음"


def test_여러_줄_기능성은_쉼표로_잇고_단서는_뺀다():
    raw = "(1) 뼈와 치아 형성에 필요\n(2) 신경과 근육 기능 유지에 필요\n※ 섭취량을 적용하지 않음"
    assert module.clean_functionality(raw) == "뼈와 치아 형성에 필요, 신경과 근육 기능 유지에 필요"

    assert module.clean_functionality("(나) 간 건강에 도움을 줄 수 있음[1) (2)의 제조방법에 한함]") == "간 건강에 도움을 줄 수 있음"


def test_같은뜻_표기로_효능_대상을_잇는다():
    """공전은 '혈행', 주장은 '혈액순환'이라고 쓴다. 잇지 않으면 오메가3 주장이 미인정이 된다."""
    targets = module.derive_targets("혈중 중성지질 개선･혈행 개선에 도움", ("중성지질", "혈행", "혈액순환"), {"혈액순환": ["혈행"]})
    assert "혈액순환" in targets and "중성지질" in targets


def test_대응표에_있는_고시형만_항목으로_만든다():
    rows = [
        {"PRDCT_NM": "마늘", "PRIMARY_FNCLTY": "혈중 콜레스테롤 개선·혈압조절에 도움을 줄 수 있음", "LAST_UPDT_DTM": "20241223"},
        {"PRDCT_NM": "마늘추출물(제2018-3호)", "PRIMARY_FNCLTY": "간 건강에 도움을 줄 수 있음"},
    ]
    entries, missing = module.build_entries(
        rows, {"마늘": "마늘", "홍삼": "홍삼"}, ("콜레스테롤", "혈압", "간")
    )

    assert entries["마늘"]["효능_대상"] == ["콜레스테롤", "혈압"]
    assert entries["마늘"]["공식_원료명"] == "마늘"
    assert missing == ["홍삼 → 홍삼"]
