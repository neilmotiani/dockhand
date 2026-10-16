"""Access-token providers. Every Ring call gets its token through this interface, so switching
from Playground tokens to the OAuth refresh-token flow does not touch callers."""

from typing import Protocol

from app.config import Settings


class TokenUnavailable(RuntimeError):
    pass


class TokenProvider(Protocol):
    async def token(self) -> str: ...

    async def invalidate(self) -> None:
        """Called after a 401 so the next token() can fetch a fresh one."""
        ...


class PlaygroundTokenProvider:
    """A Developer Playground one-click token from RING_ACCESS_TOKEN (~30 min lifetime).

    It cannot be refreshed programmatically: after a 401 the developer pastes a new token and
    restarts. invalidate() makes that failure explicit instead of retrying a dead token.
    """

    def __init__(self, settings: Settings) -> None:
        secret = settings.ring_access_token
        self._token = secret.get_secret_value() if secret else None

    async def token(self) -> str:
        if not self._token:
            raise TokenUnavailable(
                "RING_ACCESS_TOKEN is unset or expired; generate one in the Ring Playground"
            )
        return self._token

    async def invalidate(self) -> None:
        self._token = None
