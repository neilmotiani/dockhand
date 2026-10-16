from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app


def test_healthz() -> None:
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_settings_read_from_env(monkeypatch) -> None:
    monkeypatch.setenv("BEDROCK_MODEL_ID", "test-model")
    monkeypatch.setenv("RING_ACCESS_TOKEN", "secret-token")
    monkeypatch.setenv("MEDIA_SOURCE", "own_footage")
    settings = Settings(_env_file=None)
    assert settings.bedrock_model_id == "test-model"
    assert settings.ring_access_token.get_secret_value() == "secret-token"
    assert settings.media_source == "own_footage"
    # Secrets must not leak through repr/logging.
    assert "secret-token" not in repr(settings)


def test_settings_reject_unknown_media_source(monkeypatch) -> None:
    import pydantic
    import pytest

    monkeypatch.setenv("MEDIA_SOURCE", "youtube")
    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None)


def test_bedrock_defaults_nova_with_sonnet_comparison() -> None:
    s = Settings(_env_file=None)
    assert s.bedrock_model_id == "us.amazon.nova-pro-v1:0"
    assert s.bedrock_structured_output is False
    assert s.bedrock_comparison_model_id == "us.anthropic.claude-sonnet-4-6"
    assert s.bedrock_comparison_structured_output is True
