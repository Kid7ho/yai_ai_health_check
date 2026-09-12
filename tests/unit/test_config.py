"""설정 로딩 단위 테스트. 키가 로그로 새지 않는지도 확인한다."""

from __future__ import annotations

import pytest

from healthcheck.config import Settings, load_dotenv, load_settings


def test_기본값은_fake_모드다(monkeypatch):
    """키가 없어도 서비스가 돌아가야 한다."""
    monkeypatch.delenv("ADAPTER_MODE", raising=False)
    monkeypatch.delenv("LINER_API_KEY", raising=False)
    settings = load_settings(env_file=None)
    assert settings.adapter_mode == "fake"
    assert settings.has_liner_key is False


def test_환경_변수에서_키를_읽는다(monkeypatch):
    monkeypatch.setenv("LINER_API_KEY", "test-key-abc")
    settings = load_settings(env_file=None)
    assert settings.liner_api_key == "test-key-abc"
    assert settings.has_liner_key is True


def test_키_이름만_쓰고_값이_비면_없는_것으로_본다(monkeypatch):
    monkeypatch.setenv("LINER_API_KEY", "   ")
    assert load_settings(env_file=None).has_liner_key is False


def test_잘못된_모드는_거부한다(monkeypatch):
    monkeypatch.setenv("ADAPTER_MODE", "production")
    with pytest.raises(ValueError, match="ADAPTER_MODE"):
        load_settings(env_file=None)


def test_키_값이_repr에_나오지_않는다():
    """실수로 설정 객체를 로그에 찍어도 키가 새지 않아야 한다."""
    settings = Settings(liner_api_key="secret-value-xyz", mfds_api_key="another-secret")
    assert "secret-value-xyz" not in repr(settings)
    assert "another-secret" not in repr(settings)


def test_기동_로그_요약에도_키가_들어가지_않는다():
    settings = Settings(liner_api_key="secret-value-xyz")
    summary = settings.masked()
    assert summary["liner_api_key"] == "설정됨"
    assert "secret-value-xyz" not in str(summary)


def test_dotenv가_기존_환경_변수를_덮어쓰지_않는다(tmp_path, monkeypatch):
    """배포 환경에서 주입한 값이 파일에 밀리면 안 된다."""
    monkeypatch.setenv("LINER_API_KEY", "from-environment")
    env_file = tmp_path / ".env"
    env_file.write_text("LINER_API_KEY=from-file\n", encoding="utf-8")

    load_dotenv(env_file)
    assert load_settings(env_file=env_file).liner_api_key == "from-environment"


def test_dotenv_주석과_빈_줄을_건너뛴다(tmp_path, monkeypatch):
    monkeypatch.delenv("LINER_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 주석\n\nLINER_API_KEY='quoted-key'\n잘못된줄\n", encoding="utf-8"
    )
    assert load_settings(env_file=env_file).liner_api_key == "quoted-key"


def test_env_파일이_없어도_예외가_없다(tmp_path):
    load_dotenv(tmp_path / "없는파일.env")
