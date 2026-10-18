"""Helpers for the MCP server tests: an app on a real Postgres, reached over Streamable HTTP
through an in-process ASGI transport (the same wire format a remote client sees)."""

import contextlib
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.config import Settings
from mcp_server.server import create_app

TOKEN = "mcp-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = "http://test"
# What a 2025-11-25 client sends on every request after initialize.
HEADERS = {
    **AUTH,
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-11-25",
}


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def settings(media_dir: Path, **over) -> Settings:
    base = {
        "_env_file": None,
        "mcp_token": TOKEN,
        "media_dir": media_dir,
        "mcp_public_url": BASE,
        "business_timezone": "America/Los_Angeles",
    }
    return Settings(**{**base, **over})


@contextlib.asynccontextmanager
async def http_app(engine, media_dir: Path, clock, **over) -> AsyncIterator[httpx2.AsyncClient]:
    """A raw HTTP client on the MCP app (no default headers: tests set them per request)."""
    app = create_app(settings(media_dir, **over), engine=engine, clock=clock)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=BASE
        ) as http:
            yield http


@contextlib.asynccontextmanager
async def mcp_client(
    engine, media_dir: Path, clock, **over
) -> AsyncIterator[tuple[Client, httpx2.AsyncClient]]:
    """An SDK client that negotiates 2025-11-25 with the initialize handshake."""
    async with http_app(engine, media_dir, clock, **over) as http:
        authed = httpx2.AsyncClient(
            transport=http._transport, base_url=BASE, headers=AUTH, timeout=10
        )
        async with authed:
            transport = streamable_http_client(f"{BASE}/mcp", http_client=authed)
            async with Client(transport, mode="legacy") as client:
                yield client, http
