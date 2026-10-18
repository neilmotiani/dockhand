"""Dockhand MCP server: the receiving ledger as MCP tools over Streamable HTTP.

Run: `uv run uvicorn --factory mcp_server.server:create_app --port 8020` (MCP endpoint `/mcp`).

Protocol: MCP 2025-11-25 through the official Python SDK (`mcp` 2.x), which also negotiates
older handshake versions and speaks 2026-07-28. Choices, each explained in docs/decisions.md:
- Stateless Streamable HTTP with JSON responses. No tool sends progress or server-to-client
  requests, so a session or an SSE stream would only add state that a restart loses.
  GET (open an SSE stream) and DELETE (end a session) are answered 405, as the spec allows.
- A static bearer token (MCP_TOKEN) on every request, compared in constant time. MCP
  authorization is OPTIONAL in the spec; its OAuth 2.1 flow needs an authorization server we
  don't run. 401s carry `WWW-Authenticate: Bearer`.
- Origin and Host validation (DNS-rebinding protection), from the SDK.
- Unknown tool names are a JSON-RPC protocol error (-32602), as the spec's tools page says. The
  SDK alone answers them with an `isError` result, so a middleware checks first.
- Snapshot images are signed, expiring URLs served by this process (mcp_server/media.py).
"""

import asyncio
import contextlib
import hmac
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INVALID_PARAMS,
    Annotations,
    CallToolResult,
    ResourceLink,
    TextContent,
    ToolAnnotations,
)
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings, get_settings
from app.db.models import deliveries
from app.db.session import make_engine
from app.extract.schemas import Carrier
from app.ingest.queue import utcnow
from mcp_server import tools
from mcp_server.media import resolve_media, verify
from mcp_server.speech import BadArgument

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"
SERVER_VERSION = "0.1.0"
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# Adds a row; repeating the same call adds nothing (deterministic ID, see tools.order_id).
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
MEDIA_CACHE = "private, max-age=300"


class RateLimiter:
    """Sliding one-minute window over all tool calls (the spec says servers MUST rate limit
    tool invocations). One process, one business: a single shared window is enough."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute, self._clock, self._calls = per_minute, clock, deque[float]()

    def allow(self) -> bool:
        now = self._clock()
        while self._calls and now - self._calls[0] >= 60:
            self._calls.popleft()
        if len(self._calls) >= self.per_minute:
            return False
        self._calls.append(now)
        return True


def _result(out: tools.ToolOutput) -> CallToolResult:
    """Speakable summary first, then the structured result serialized as JSON (the spec asks
    for both when a tool returns structuredContent), then one resource link per snapshot."""
    data = out.model_dump(mode="json")
    links, seen = [], set()
    for card in out.cards:
        if card.image_url and card.image_url not in seen:
            seen.add(card.image_url)
            links.append(
                ResourceLink(
                    uri=card.image_url,
                    name=f"Snapshot: {card.title}",
                    mime_type="image/jpeg",
                    annotations=Annotations(audience=["user"]),
                )
            )
    return CallToolResult(
        content=[
            TextContent(text=out.summary),
            TextContent(text=json.dumps(data, separators=(",", ":"))),
            *links,
        ],
        structured_content=data,
    )


def _error(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(text=message)], is_error=True)


def build_server(ledger: tools.Ledger, limiter: RateLimiter) -> MCPServer:
    async def run(call: Awaitable[tools.ToolOutput]) -> CallToolResult:
        if not limiter.allow():
            call.close()  # type: ignore[attr-defined]  # the coroutine is never awaited
            return _error("Too many tool calls; try again in a minute.")
        try:
            return _result(await call)
        except BadArgument as e:
            # A tool execution error, not a protocol error: the model reads it and retries.
            return _error(str(e))

    names: set[str] = set()

    async def unknown_tool_guard(ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        if ctx.method == "tools/call":
            name = (ctx.params or {}).get("name")
            if name not in names:
                raise MCPError(INVALID_PARAMS, f"Unknown tool: {name}")
        return await call_next(ctx)

    mcp = MCPServer(
        name="dockhand",
        title="Dockhand receiving ledger",
        version=SERVER_VERSION,
        instructions=(
            "Dockhand logs deliveries seen by a Ring camera at a business's back door and "
            "reconciles them against expected orders. Times are in "
            f"{ledger.settings.business_timezone}. Every tool returns a `summary` written to be "
            "spoken, `cards` for a screen (with signed snapshot URLs that expire), and typed "
            "rows. If `includes_simulated` is true, say the answer includes demo data."
        ),
        middleware=[unknown_tool_guard],
    )

    DateArg = Annotated[
        str | None,
        Field(description="Business day as YYYY-MM-DD, or 'today', 'yesterday', 'tomorrow'"),
    ]

    @mcp.tool(
        title="Today's deliveries",
        description=(
            "Deliveries logged on a business day (default today), each with its status "
            "(matched, short, over, unexpected), plus the day's expected orders and whether "
            "they are pending or missing."
        ),
        annotations=READ_ONLY,
    )
    async def get_todays_deliveries(
        date: DateArg = None,
    ) -> Annotated[CallToolResult, tools.DeliveriesOutput]:
        return await run(tools.get_todays_deliveries(ledger, date=date))

    @mcp.tool(
        title="Check a delivery",
        description=(
            "Did a supplier's delivery arrive? Look up an expected order by supplier name "
            "(spoken or misspelled names are matched to known suppliers) or by order "
            "reference. Returns its status, arrival time, box count and snapshot. Without a "
            "date it picks today's order, else the most recent one."
        ),
        annotations=READ_ONLY,
    )
    async def check_delivery(
        supplier: Annotated[str | None, Field(description="Supplier name, e.g. Sysco")] = None,
        reference: Annotated[str | None, Field(description="PO or order number")] = None,
        date: DateArg = None,
    ) -> Annotated[CallToolResult, tools.CheckOutput]:
        return await run(
            tools.check_delivery(ledger, supplier=supplier, reference=reference, date=date)
        )

    @mcp.tool(
        title="List discrepancies",
        description=(
            "Short, over, missing and unexpected deliveries with reasons, most urgent first, "
            f"for a date range of up to {tools.MAX_RANGE_DAYS} days (default today only)."
        ),
        annotations=READ_ONLY,
    )
    async def list_discrepancies(
        start_date: DateArg = None,
        end_date: Annotated[
            str | None, Field(description="Last day, inclusive (default: start_date)")
        ] = None,
    ) -> Annotated[CallToolResult, tools.DiscrepanciesOutput]:
        return await run(tools.list_discrepancies(ledger, start_date, end_date))

    @mcp.tool(
        title="Add an expected delivery",
        description=(
            "Add an order the business expects: supplier, box count and a delivery window on "
            "one day (times in the business's timezone). Carrier and reference are optional. "
            "Repeating the same call adds the order once."
        ),
        annotations=WRITE,
    )
    async def add_expected_delivery(
        supplier: Annotated[str, Field(min_length=1, max_length=200)],
        count: Annotated[int, Field(description="Number of boxes", ge=1, le=10_000)],
        date: Annotated[str, Field(description="YYYY-MM-DD, 'today' or 'tomorrow'")],
        window_start: Annotated[str, Field(description="Local time, e.g. 07:00 or 7:00 AM")],
        window_end: Annotated[str, Field(description="Local time, e.g. 09:00 or 9:00 AM")],
        carrier: Annotated[
            Carrier | None, Field(description="Who drops it off; omit if unknown")
        ] = None,
        reference: Annotated[str | None, Field(max_length=200)] = None,
    ) -> Annotated[CallToolResult, tools.AddOutput]:
        return await run(
            tools.add_expected_delivery(
                ledger, supplier, count, date, window_start, window_end, carrier, reference
            )
        )

    @mcp.tool(
        title="Supplier scorecard",
        description=(
            "Per-supplier reliability over recent days: orders on time, short deliveries, "
            "boxes short and missed deliveries. Names the supplier that shorts the business "
            "the most and the one with the most problems overall."
        ),
        annotations=READ_ONLY,
    )
    async def get_supplier_scorecard(
        supplier: Annotated[str | None, Field(description="One supplier; omit for all")] = None,
        days: Annotated[int, Field(ge=1, le=365, description="Look-back window")] = 30,
    ) -> Annotated[CallToolResult, tools.ScorecardOutput]:
        return await run(tools.get_supplier_scorecard(ledger, supplier, days))

    names.update(
        f.__name__
        for f in (
            get_todays_deliveries,
            check_delivery,
            list_discrepancies,
            add_expected_delivery,
            get_supplier_scorecard,
        )
    )
    return mcp


class BearerGuard:
    """Require `Authorization: Bearer $MCP_TOKEN` on the MCP endpoint, then answer GET and
    DELETE with 405 (stateless server: no stream to open, no session to end)."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app, self._expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") != MCP_PATH:
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        given = headers.get(b"authorization", b"")
        if not hmac.compare_digest(given, self._expected):
            challenge = 'Bearer realm="dockhand-mcp"'
            if given:
                challenge += ', error="invalid_token"'
            res: Response = JSONResponse(
                {"error": "unauthorized", "detail": "Authorization: Bearer <MCP_TOKEN> required"},
                status_code=401,
                headers={"WWW-Authenticate": challenge},
            )
        elif scope["method"] in ("GET", "DELETE"):
            res = Response(status_code=405, headers={"Allow": "POST"})
        else:
            await self.app(scope, receive, send)
            return
        await res(scope, receive, send)


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    """The server's own public host and origin are always allowed: a same-origin request is
    not a rebinding attack, because the Host check already pins who is being talked to."""
    hosts, origins = list(settings.mcp_allowed_hosts), list(settings.mcp_allowed_origins)
    public = urlsplit(settings.mcp_public_url)
    if public.netloc and public.netloc not in hosts:
        hosts.append(public.netloc)
    if public.netloc and f"{public.scheme}://{public.netloc}" not in origins:
        origins.append(f"{public.scheme}://{public.netloc}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def create_app(
    settings: Settings | None = None,
    *,
    engine: AsyncEngine | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> Starlette:
    settings = settings or get_settings()
    if settings.mcp_token is None:
        raise RuntimeError("MCP_TOKEN is not set: the MCP server does not run without a token")
    own_engine = engine is None
    engine = engine or make_engine(settings.database_url)
    ledger = tools.Ledger(engine, settings, clock)
    mcp = build_server(ledger, RateLimiter(settings.mcp_rate_limit_per_min))

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/media/deliveries/{delivery_id}/{kind}", methods=["GET"])
    async def media(request: Request) -> Response:
        try:
            delivery_id = uuid.UUID(request.path_params["delivery_id"])
        except ValueError:
            return Response("Not found", status_code=404)
        kind = request.path_params["kind"]
        q = request.query_params
        if not verify(ledger.key, delivery_id, kind, q.get("exp", ""), q.get("sig", ""), clock()):
            return Response("Link expired or invalid", status_code=403)
        async with engine.connect() as conn:
            path = (
                await conn.execute(
                    select(deliveries.c.snapshot_path).where(deliveries.c.id == delivery_id)
                )
            ).scalar_one_or_none()
        file = resolve_media(path, Path(settings.media_dir))
        if file is None:
            return Response("Not found", status_code=404)
        return FileResponse(file, media_type="image/jpeg", headers={"Cache-Control": MEDIA_CACHE})

    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=_transport_security(settings),
    )
    inner_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a: Starlette) -> AsyncIterator[None]:
        async with inner_lifespan(a):
            try:
                yield
            finally:
                if own_engine:
                    await asyncio.shield(engine.dispose())

    app.router.lifespan_context = lifespan
    app.add_middleware(BearerGuard, token=settings.mcp_token.get_secret_value())
    return app
