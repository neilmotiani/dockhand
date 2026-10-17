"""Access-token providers. Every Ring call gets its token through this interface, so switching
from Playground tokens to the OAuth refresh-token flow does not touch callers."""

import base64
import binascii
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from dotenv import dotenv_values

from app.config import Settings

log = logging.getLogger(__name__)

ENV_KEY = "RING_ACCESS_TOKEN"


class TokenUnavailable(RuntimeError):
    pass


class TokenProvider(Protocol):
    async def token(self) -> str: ...

    async def invalidate(self) -> None:
        """Called after a 401 so the next token() can fetch a fresh one."""
        ...


def jwt_expiry(token: str) -> float | None:
    """The `exp` claim (epoch seconds) of a JWT, or None if the token isn't one.

    Read without verifying the signature: it only decides whether a call is worth making. Ring
    still checks the token. Playground tokens are JWTs with a 30-minute exp - iat.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        exp = json.loads(payload).get("exp")
    except (binascii.Error, ValueError, AttributeError):
        return None
    return float(exp) if isinstance(exp, int | float) and not isinstance(exp, bool) else None


class PlaygroundTokenProvider:
    """A Developer Playground one-click token from RING_ACCESS_TOKEN (30 min lifetime).

    Playground tokens can't be refreshed programmatically, so the developer pastes a new one
    into `.env`. The token is re-read on every call, so the new one is used without
    restarting the API. Precedence matches Settings: an exported environment variable wins
    over `.env`, and the value loaded at startup is the fallback.

    After a 401, invalidate() marks that token dead and token() refuses it until a different
    one appears, so a rejected token isn't retried. A token whose `exp` has passed is refused
    the same way, without a call to Ring.
    """

    def __init__(
        self,
        settings: Settings,
        env_file: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        secret = settings.ring_access_token
        self._startup = secret.get_secret_value() if secret else None
        self._env_file = env_file
        self._clock = clock
        self._dead: str | None = None
        self._last: str | None = None

    def _current(self) -> str | None:
        if ENV_KEY in os.environ:
            value = os.environ[ENV_KEY]
        elif self._env_file is not None and self._env_file.is_file():
            value = dotenv_values(self._env_file).get(ENV_KEY)
        else:
            value = self._startup
        return (value or "").strip() or None

    async def token(self) -> str:
        tok = self._current()
        if not tok:
            raise TokenUnavailable(
                f"{ENV_KEY} is unset; generate a token in the Ring Playground and put it in .env"
            )
        if tok == self._dead:
            raise TokenUnavailable(
                f"Ring rejected {ENV_KEY} (401); paste a fresh Playground token into .env"
            )
        exp = jwt_expiry(tok)
        if exp is not None and exp <= self._clock():
            at = datetime.fromtimestamp(exp, UTC).strftime("%H:%M UTC")
            raise TokenUnavailable(
                f"{ENV_KEY} expired at {at}; paste a fresh Playground token into .env"
            )
        if tok != self._last:
            self._last = tok
            until = f", valid until {datetime.fromtimestamp(exp, UTC):%H:%M} UTC" if exp else ""
            log.info("Ring token loaded%s", until)
        return tok

    async def invalidate(self) -> None:
        self._dead = self._current()
