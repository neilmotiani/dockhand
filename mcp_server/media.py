"""Signed, expiring snapshot links for tool results.

A screen device (an Echo Show, or the dashboard's browser) loads a card's image by URL and
cannot add an `Authorization` header, so the URL itself carries the permission: an HMAC over
(delivery ID, kind, expiry). It names one file and stops working after MCP_MEDIA_TTL_S. Nothing
else is reachable through it, and the MCP bearer token never appears in a URL (the MCP spec
forbids access tokens in query strings).
"""

import hashlib
import hmac
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from app.config import Settings

KINDS = ("snapshot",)


def media_key(settings: Settings) -> bytes:
    if settings.mcp_media_key is not None:
        return settings.mcp_media_key.get_secret_value().encode()
    if settings.mcp_token is None:
        raise ValueError("MCP_TOKEN is not set")
    # Derived rather than reused, so a leaked link never reveals anything about the token.
    token = settings.mcp_token.get_secret_value().encode()
    return hmac.new(token, b"dockhand-mcp-media", hashlib.sha256).digest()


def _sig(key: bytes, delivery_id: uuid.UUID, kind: str, exp: int) -> str:
    msg = f"{delivery_id}:{kind}:{exp}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def signed_url(
    base_url: str, key: bytes, delivery_id: uuid.UUID, now: datetime, ttl_s: int, kind="snapshot"
) -> str:
    exp = int(now.timestamp()) + ttl_s
    qs = urlencode({"exp": exp, "sig": _sig(key, delivery_id, kind, exp)})
    return f"{base_url.rstrip('/')}/media/deliveries/{delivery_id}/{kind}?{qs}"


def verify(
    key: bytes, delivery_id: uuid.UUID, kind: str, exp: str, sig: str, now: datetime
) -> bool:
    if kind not in KINDS or not exp.isdigit() or int(exp) < now.timestamp():
        return False
    return hmac.compare_digest(_sig(key, delivery_id, kind, int(exp)), sig)


def resolve_media(path: str | None, media_dir: Path) -> Path | None:
    """The file at `path` if it exists and lies under MEDIA_DIR (paths come from our own
    database, but only ever serve evidence files)."""
    if not path:
        return None
    p = Path(path).resolve()
    if not p.is_relative_to(media_dir.resolve()) or not p.is_file():
        return None
    return p
