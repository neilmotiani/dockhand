import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_healthz() -> None:
    # No `with`: lifespan (database, workers) is not needed for the health check.
    client = TestClient(create_app(Settings(_env_file=None)))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_settings_read_from_env(monkeypatch) -> None:
    monkeypatch.setenv("BEDROCK_MODEL_ID", "test-model")
    monkeypatch.setenv("RING_ACCESS_TOKEN", "secret-token")
    monkeypatch.setenv("MEDIA_SOURCE", "ring_device")
    settings = Settings(_env_file=None)
    assert settings.bedrock_model_id == "test-model"
    assert settings.ring_access_token.get_secret_value() == "secret-token"
    assert settings.media_source == "ring_device"
    # Secrets must not leak through repr/logging.
    assert "secret-token" not in repr(settings)


@pytest.mark.parametrize("value", ["youtube", "own_footage"])
def test_settings_reject_unknown_media_source(monkeypatch, value) -> None:
    import pydantic

    # own_footage was removed: there is no self-filmed footage in this project.
    monkeypatch.setenv("MEDIA_SOURCE", value)
    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None)


def test_bedrock_defaults_nova_with_sonnet_comparison() -> None:
    s = Settings(_env_file=None)
    assert s.bedrock_model_id == "us.amazon.nova-pro-v1:0"
    assert s.bedrock_structured_output is False
    assert s.bedrock_comparison_model_id == "us.anthropic.claude-sonnet-4-6"
    assert s.bedrock_comparison_structured_output is True


def test_blank_secrets_are_unset(monkeypatch) -> None:
    for key in ("RING_WEBHOOK_SECRET", "ADMIN_TOKEN", "RING_ACCESS_TOKEN"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "  ")
    monkeypatch.setenv("CAPTURE_REFERENCE_DIR", "")
    s = Settings(_env_file=None)
    assert s.capture_reference_dir is None
    assert s.ring_webhook_secret is None
    assert s.admin_token is None
    assert s.ring_access_token is None
    assert s.aws_bearer_token_bedrock is None


def test_relevant_events_parse_from_json_env(monkeypatch) -> None:
    monkeypatch.setenv("INGEST_RELEVANT_EVENTS", '["button_press"]')
    assert Settings(_env_file=None).ingest_relevant_events == ["button_press"]
