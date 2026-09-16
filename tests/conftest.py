"""테스트 공통 설정.

테스트가 개발자의 .env를 읽지 않게 한다. 지금까지는 .env에 값이 없어서
우연히 통과했지만, 실제 키와 토큰을 채우자 테스트가 깨졌다. 테스트 결과가
각자의 로컬 설정에 따라 달라지면 안 된다.

load_dotenv는 이미 설정된 환경 변수를 덮어쓰지 않으므로, 여기서 먼저
값을 정해 두면 .env가 끼어들지 못한다.
"""

from __future__ import annotations

import pytest

# .env가 제공할 수 있는 값 전부를 테스트 기본값으로 고정한다.
TEST_ENV = {
    "ADAPTER_MODE": "fake",
    "LINER_API_KEY": "",
    "MFDS_API_KEY": "",
    "YOUTUBE_PROXY_URL": "",
    "WEBSHARE_PROXY_USERNAME": "",
    "WEBSHARE_PROXY_PASSWORD": "",
    "SKILL_AUTH_TOKEN": "",
    "SKILL_AUTH_HEADER": "X-Skill-Token",
    "SKILL_PATH": "/skill",
    "DEBUG_UI": "1",
    "LOG_SKILL_REQUEST": "0",
    "ASR_ENABLED": "0",
    # 개발자 .env의 Gemini 키로 실제 호출하지 않게 막는다.
    "GEMINI_API_KEY": "",
    # 개발자 .env에 분류 시트 토큰이 있어도 테스트 앱에는 붙이지 않는다.
    "LABELING_TOKEN": "",
    "GEMINI_VIDEO_ENABLED": "0",
    "ARTICLE_ENABLED": "1",
    # 테스트가 실제 DB 파일을 만들지 않게 한다.
    "DATABASE_PATH": ":memory:",
    "WORKER_COUNT": "2",
}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
