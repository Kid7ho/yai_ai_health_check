"""팀원 분류 시트 서버 테스트."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from healthcheck.api.labeling import LabelingStore, build_router

TOKEN = "t0ken-for-test"


def client_for(tmp_path) -> TestClient:
    sheet = tmp_path / "sheet.html"
    sheet.write_text("<title>시트</title><p>본문</p>", encoding="utf-8")
    app = FastAPI()
    app.include_router(build_router(TOKEN, LabelingStore(tmp_path / "labeling.db"), sheet_path=sheet))
    return TestClient(app)


def test_토큰이_틀리면_시트도_API도_없는_주소다(tmp_path):
    client = client_for(tmp_path)
    assert client.get("/labeling/wrong/").status_code == 404
    assert client.get("/labeling/wrong/api/state").status_code == 404
    assert client.put("/labeling/wrong/api/members/m1", json={"name": "a"}).status_code == 404


def test_시트에_서버_저장소_주소를_넣어_돌려준다(tmp_path):
    response = client_for(tmp_path).get(f"/labeling/{TOKEN}/")
    assert response.status_code == 200
    assert f'window.LABELING_API = "/labeling/{TOKEN}/api"' in response.text
    assert response.text.startswith("<!doctype html>")
    assert response.headers["cache-control"] == "no-store"


def test_분류와_팀원_이름을_저장하고_다시_읽는다(tmp_path):
    client = client_for(tmp_path)
    body = {"caseId": "lab-001", "slot": "m2", "claims": [{"label": "CAUTION"}]}
    assert client.put(f"/labeling/{TOKEN}/api/labels/lab-001__m2", json=body).status_code == 200
    assert client.put(f"/labeling/{TOKEN}/api/members/m2", json={"name": "홍길동"}).status_code == 200
    body["claims"][0]["label"] = "WARNING"
    client.put(f"/labeling/{TOKEN}/api/labels/lab-001__m2", json=body)

    state = client.get(f"/labeling/{TOKEN}/api/state").json()
    assert state["labels"]["lab-001__m2"]["claims"][0]["label"] == "WARNING"
    assert state["members"] == {"m2": {"name": "홍길동"}}


def test_키_형식과_팀원_번호를_확인한다(tmp_path):
    client = client_for(tmp_path)
    assert client.put(f"/labeling/{TOKEN}/api/labels/..__m1", json={"slot": "m1"}).status_code == 400
    # 키의 팀원 번호와 본문의 번호가 다르면 다른 사람의 분류를 덮어쓸 수 있다
    assert client.put(f"/labeling/{TOKEN}/api/labels/lab-001__m1", json={"slot": "m3"}).status_code == 400
    assert client.put(f"/labeling/{TOKEN}/api/members/m9", json={"name": "a"}).status_code == 400
    assert client.put(f"/labeling/{TOKEN}/api/labels/lab-001__m1", content=b"[1]").status_code == 400
