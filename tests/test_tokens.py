import base64
import json

import pytest

from app.config import Settings
from app.ring.tokens import PlaygroundTokenProvider, TokenUnavailable, jwt_expiry

NOW = 1_800_000_000.0


def jwt(exp: float | None, tag: str = "a") -> str:
    """Unsigned JWT-shaped token; only the payload matters here."""

    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    claims = {"iat": NOW - 60, "tag": tag}
    if exp is not None:
        claims["exp"] = exp
    return f"{b64({'alg': 'none'})}.{b64(claims)}.sig"


@pytest.fixture(autouse=True)
def no_exported_token(monkeypatch):
    monkeypatch.delenv("RING_ACCESS_TOKEN", raising=False)


def provider(env_file=None, startup: str | None = None) -> PlaygroundTokenProvider:
    s = Settings(_env_file=None, ring_access_token=startup)
    return PlaygroundTokenProvider(s, env_file=env_file, clock=lambda: NOW)


def write_env(path, token: str) -> None:
    path.write_text(f"ADMIN_TOKEN=x\nRING_ACCESS_TOKEN={token}\nOTHER=1\n")


async def test_fresh_token_in_env_file_is_used_without_restart(tmp_path):
    env = tmp_path / ".env"
    old, new = jwt(NOW + 600, "old"), jwt(NOW + 1800, "new")
    write_env(env, old)
    p = provider(env, startup=old)
    assert await p.token() == old
    write_env(env, new)
    assert await p.token() == new


async def test_rejected_token_is_not_retried_until_replaced(tmp_path):
    env = tmp_path / ".env"
    dead, fresh = jwt(NOW + 600, "dead"), jwt(NOW + 1800, "fresh")
    write_env(env, dead)
    p = provider(env)
    assert await p.token() == dead
    await p.invalidate()  # Ring answered 401
    with pytest.raises(TokenUnavailable, match="401"):
        await p.token()
    write_env(env, fresh)
    assert await p.token() == fresh


async def test_expired_token_is_refused_without_calling_ring(tmp_path):
    env = tmp_path / ".env"
    write_env(env, jwt(NOW - 1))
    with pytest.raises(TokenUnavailable, match="expired at"):
        await provider(env).token()


async def test_last_duplicate_key_wins_like_settings(tmp_path):
    env = tmp_path / ".env"
    a, b = jwt(NOW + 600, "a"), jwt(NOW + 600, "b")
    env.write_text(f"RING_ACCESS_TOKEN={a}\nRING_ACCESS_TOKEN={b}\n")
    assert await provider(env).token() == b


async def test_exported_variable_wins_over_env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    write_env(env, jwt(NOW + 600, "file"))
    exported = jwt(NOW + 600, "exported")
    monkeypatch.setenv("RING_ACCESS_TOKEN", exported)
    assert await provider(env).token() == exported


async def test_falls_back_to_startup_value_and_reports_unset(tmp_path):
    startup = jwt(NOW + 600, "startup")
    assert await provider(tmp_path / "missing.env", startup=startup).token() == startup
    assert await provider(None, startup=startup).token() == startup
    env = tmp_path / ".env"
    env.write_text("RING_ACCESS_TOKEN=\n")
    with pytest.raises(TokenUnavailable, match="unset"):
        await provider(env).token()


async def test_non_jwt_token_is_passed_through(tmp_path):
    env = tmp_path / ".env"
    write_env(env, "opaque-token")
    assert await provider(env).token() == "opaque-token"


def test_jwt_expiry():
    assert jwt_expiry(jwt(NOW + 5)) == NOW + 5
    assert jwt_expiry(jwt(None)) is None
    assert jwt_expiry("a.b") is None
    assert jwt_expiry("a.!!!.c") is None
    assert jwt_expiry("a.bm90IGpzb24.c") is None  # "not json"
