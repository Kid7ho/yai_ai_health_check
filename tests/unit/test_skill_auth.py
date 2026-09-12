"""스킬 엔드포인트 인증 테스트.

스킬 서버는 공개 https 주소로 노출해야 한다. 인증이 없으면 주소를 아는
누구나 파이프라인을 호출하고 Liner 토큰이 남의 요청에 쓰인다.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from healthcheck.api.app import build_fake_orchestrator, create_app
from healthcheck.api.auth import SkillAuth

TOKEN = "test-token-abcdef"
BODY = {
    "userRequest": {
        "utterance": "홍삼이 면역력에 좋다",
        "callbackUrl": "https://callback.kakao.example/abc",
        "user": {"id": "botuser-1"},
    }
}


def app_with(auth: SkillAuth, skill_path: str = "/skill"):
    return create_app(
        build_fake_orchestrator(), worker_count=0, auth=auth, skill_path=skill_path
    )


# ── 헤더 토큰 ────────────────────────────────────────────────


def test_토큰이_맞으면_처리한다():
    app = app_with(SkillAuth(token=TOKEN))
    with TestClient(app) as client:
        response = client.post("/skill", json=BODY, headers={"X-Skill-Token": TOKEN})

    assert response.status_code == 200
    assert response.json()["useCallback"] is True


def test_토큰이_없으면_거부한다():
    app = app_with(SkillAuth(token=TOKEN))
    with TestClient(app) as client:
        response = client.post("/skill", json=BODY)

    assert response.status_code == 401
    assert app.state.queue.size == 0


def test_토큰이_틀리면_거부한다():
    app = app_with(SkillAuth(token=TOKEN))
    with TestClient(app) as client:
        response = client.post("/skill", json=BODY, headers={"X-Skill-Token": "wrong"})

    assert response.status_code == 401


def test_거부된_요청은_파이프라인을_돌리지_않는다():
    """Liner 토큰과 큐를 남의 요청에 쓰지 않는다."""
    orchestrator = build_fake_orchestrator()
    app = create_app(orchestrator, worker_count=0, auth=SkillAuth(token=TOKEN))

    with TestClient(app) as client:
        client.post("/skill", json=BODY)

    assert orchestrator.transcript.calls == []
    assert app.state.queue.size == 0


def test_헤더_이름을_바꿀_수_있다():
    app = app_with(SkillAuth(header_name="X-My-Secret", token=TOKEN))
    with TestClient(app) as client:
        assert client.post("/skill", json=BODY, headers={"X-My-Secret": TOKEN}).status_code == 200
        assert client.post("/skill", json=BODY, headers={"X-Skill-Token": TOKEN}).status_code == 401


def test_토큰을_설정하지_않으면_통과시킨다():
    """로컬 개발에서 매번 헤더를 붙이지 않아도 되게 한다."""
    app = app_with(SkillAuth())
    with TestClient(app) as client:
        assert client.post("/skill", json=BODY).status_code == 200


def test_토큰_값이_설정_출력에_나오지_않는다():
    assert TOKEN not in repr(SkillAuth(token=TOKEN))


# ── 비밀 경로 ────────────────────────────────────────────────


def test_경로를_바꿀_수_있다():
    """오픈빌더가 임의 헤더를 지원하지 않을 때 쓰는 대체 수단이다."""
    app = app_with(SkillAuth(), skill_path="/skill/3f9a2c7b1d8e")
    with TestClient(app) as client:
        assert client.post("/skill/3f9a2c7b1d8e", json=BODY).status_code == 200
        assert client.post("/skill", json=BODY).status_code == 404


def test_경로_설정이_슬래시를_빠뜨려도_동작한다():
    from healthcheck.config import _normalize_path

    assert _normalize_path("skill/abc") == "/skill/abc"
    assert _normalize_path("/skill/abc/") == "/skill/abc"
    assert _normalize_path(None) == "/skill"
    assert _normalize_path("") == "/skill"


# ── 지표 엔드포인트도 보호한다 ───────────────────────────────


def test_지표도_토큰으로_보호한다():
    """집계값뿐이지만 서비스 이용량이 드러난다."""
    app = app_with(SkillAuth(token=TOKEN))
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"X-Skill-Token": TOKEN}).status_code == 200


def test_health는_토큰_없이_열어_둔다():
    """상태 확인은 외부 감시 도구가 토큰 없이 호출해야 한다."""
    app = app_with(SkillAuth(token=TOKEN))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
