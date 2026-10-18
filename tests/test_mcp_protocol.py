"""MCP protocol conformance at the wire level, against spec revision 2025-11-25.

Raw JSON-RPC over HTTP (no SDK client), so each test checks what the spec text says a server
MUST or SHOULD do, quoted next to the assertion. Pages: basic/lifecycle, basic/transports,
basic/authorization, server/tools. The official conformance suite (scripts/mcp_conformance.py)
runs against a live server on top of this.
"""

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from app.config import Settings
from mcp_server.server import create_app
from tests.mcp_fixtures import AUTH, HEADERS, TOKEN, MutableClock, http_app
from tests.pipeline_fakes import T0

NOW = T0 + timedelta(hours=1)
SPEC = "2025-11-25"


@pytest.fixture
def media(tmp_path) -> Path:
    p = tmp_path / "media"
    p.mkdir()
    return p


def rpc(method: str, params: dict | None = None, id: int | None = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if id is not None:
        msg["id"] = id
    return msg


def initialize(version: str = SPEC) -> dict:
    return rpc(
        "initialize",
        {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "conformance-test", "version": "1"},
        },
    )


async def post(http, body, headers=HEADERS):
    return await http.post("/mcp", content=json.dumps(body), headers=headers)


async def test_initialize_negotiates_2025_11_25(engine, media):
    async with http_app(engine, media, MutableClock(NOW)) as http:
        r = await post(
            http, initialize(), {k: v for k, v in HEADERS.items() if "Protocol" not in k}
        )
    assert r.status_code == 200
    # Transports: a request is answered with application/json or text/event-stream.
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 1
    result = body["result"]
    # Lifecycle: "If the server supports the requested protocol version, it MUST respond with
    # the same version."
    assert result["protocolVersion"] == SPEC
    # Tools: "Servers that support tools MUST declare the tools capability."
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "dockhand"
    assert result["serverInfo"]["version"]
    assert "instructions" in result
    # Stateless server: no MCP-Session-Id (the spec says a server MAY assign one).
    assert "mcp-session-id" not in r.headers


async def test_initialize_counter_offers_a_supported_version(engine, media):
    async with http_app(engine, media, MutableClock(NOW)) as http:
        r = await post(http, initialize("2099-01-01"), {**HEADERS, "MCP-Protocol-Version": SPEC})
    # Lifecycle: "Otherwise, the server MUST respond with another protocol version it
    # supports. This SHOULD be the latest version supported by the server."
    assert r.json()["result"]["protocolVersion"] in HANDSHAKE_PROTOCOL_VERSIONS


async def test_notification_gets_202_with_no_body(engine, media):
    async with http_app(engine, media, MutableClock(NOW)) as http:
        r = await post(http, rpc("notifications/initialized", id=None))
    # Transports: "If the server accepts the input, the server MUST return HTTP status code
    # 202 Accepted with no body."
    assert r.status_code == 202
    assert r.content == b""


async def test_ping(engine, media):
    async with http_app(engine, media, MutableClock(NOW)) as http:
        r = await post(http, rpc("ping", id=7))
    assert r.json() == {"jsonrpc": "2.0", "id": 7, "result": {}}


async def test_tools_list_schemas(engine, media):
    async with http_app(engine, media, MutableClock(NOW)) as http:
        r = await post(http, rpc("tools/list", {}))
    tools = r.json()["result"]["tools"]
    assert len(tools) == 5
    for t in tools:
        # Tools: names SHOULD be 1-128 characters of A-Z, a-z, 0-9, _, - and .
        assert re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", t["name"])
        assert t["description"] and t["title"]
        # inputSchema "MUST be a valid JSON Schema object (not null)".
        assert t["inputSchema"]["type"] == "object"
        assert t["outputSchema"]["type"] == "object"
        assert isinstance(t["annotations"]["readOnlyHint"], bool)


async def test_tools_call_result_shape(engine, media):
    async with http_app(engine, media, MutableClock(NOW)) as http:
        r = await post(
            http, rpc("tools/call", {"name": "get_todays_deliveries", "arguments": {}}, id=3)
        )
    body = r.json()
    assert body["id"] == 3
    result = body["result"]
    assert result["isError"] is False
    # Tools: "a tool that returns structured content SHOULD also return the serialized JSON in
    # a TextContent block."
    texts = [c["text"] for c in result["content"] if c["type"] == "text"]
    assert json.loads(texts[1]) == result["structuredContent"]
    assert texts[0] == result["structuredContent"]["summary"]


async def test_unknown_tool_is_a_protocol_error(engine, media):
    async with http_app(engine, media, MutableClock(NOW)) as http:
        r = await post(http, rpc("tools/call", {"name": "no_such_tool", "arguments": {}}, id=4))
    # Tools, Error Handling: unknown tools are a protocol error; the spec's example is
    # {"code": -32602, "message": "Unknown tool: invalid_tool_name"}.
    body = r.json()
    assert "result" not in body
    assert body["id"] == 4
    assert body["error"]["code"] == -32602
    assert body["error"]["message"] == "Unknown tool: no_such_tool"


async def test_bad_arguments_are_a_tool_execution_error(engine, media):
    async with http_app(engine, media, MutableClock(NOW)) as http:
        missing = await post(
            http, rpc("tools/call", {"name": "add_expected_delivery", "arguments": {}}, id=5)
        )
        invalid = await post(
            http,
            rpc("tools/call", {"name": "check_delivery", "arguments": {"date": "soon"}}, id=6),
        )
    # Tools: input validation errors are reported "in tool results with isError: true", so
    # the model can self-correct.
    for r in (missing, invalid):
        result = r.json()["result"]
        assert result["isError"] is True
        assert result["content"][0]["type"] == "text"


async def test_unknown_method(engine, media):
    async with http_app(engine, media, MutableClock(NOW)) as http:
        r = await post(http, rpc("tools/frobnicate", {}, id=8))
    assert r.json()["error"]["code"] == -32601  # JSON-RPC "Method not found"


async def test_requires_bearer_token(engine, media):
    async with http_app(engine, media, MutableClock(NOW)) as http:
        no_auth = {k: v for k, v in HEADERS.items() if k != "Authorization"}
        missing = await post(http, initialize(), no_auth)
        wrong = await post(http, initialize(), {**no_auth, "Authorization": "Bearer nope"})
        in_query = await http.post(
            f"/mcp?access_token={TOKEN}", content=json.dumps(initialize()), headers=no_auth
        )
        health = await http.get("/healthz")
    # Authorization: "Invalid or expired tokens MUST receive a HTTP 401 response", with a
    # WWW-Authenticate challenge (RFC 6750).
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Bearer ")
    assert wrong.status_code == 401
    assert 'error="invalid_token"' in wrong.headers["www-authenticate"]
    # "Access tokens MUST NOT be included in the URI query string": never accepted there.
    assert in_query.status_code == 401
    assert health.status_code == 200


async def test_get_and_delete_are_405(engine, media):
    async with http_app(engine, media, MutableClock(NOW)) as http:
        get = await http.get("/mcp", headers={**AUTH, "Accept": "text/event-stream"})
        delete = await http.delete("/mcp", headers=AUTH)
        get_anon = await http.get("/mcp", headers={"Accept": "text/event-stream"})
    # Transports: the server "MUST either return Content-Type: text/event-stream in response
    # to this HTTP GET, or else return HTTP 405 Method Not Allowed". DELETE: "The server MAY
    # respond to this request with HTTP 405 Method Not Allowed".
    assert get.status_code == 405 and delete.status_code == 405
    assert get.headers["allow"] == "POST"
    assert get_anon.status_code == 401  # authentication comes first


async def test_origin_validation(engine, media):
    async with http_app(
        engine, media, MutableClock(NOW), mcp_allowed_origins=["http://ok.test"]
    ) as http:
        evil = await post(http, initialize(), {**HEADERS, "Origin": "http://evil.example"})
        ok = await post(http, initialize(), {**HEADERS, "Origin": "http://ok.test"})
        none = await post(http, initialize())
        bad_host = await http.post(
            "/mcp", content=json.dumps(initialize()), headers={**HEADERS, "Host": "evil.example"}
        )
    # Transports: "If the Origin header is present and invalid, servers MUST respond with
    # HTTP 403 Forbidden."
    assert evil.status_code == 403
    assert ok.status_code == 200 and none.status_code == 200
    # Host validation is the other half of DNS-rebinding protection.
    assert bad_host.status_code in (400, 403, 421)


async def test_unsupported_protocol_version_header_is_400(engine, media):
    async with http_app(engine, media, MutableClock(NOW)) as http:
        r = await post(
            http, rpc("tools/list", {}), {**HEADERS, "MCP-Protocol-Version": "1999-01-01"}
        )
        ok = await post(
            http, rpc("tools/list", {}), {**HEADERS, "MCP-Protocol-Version": "2025-06-18"}
        )
    # Transports: "If the server receives a request with an invalid or unsupported
    # MCP-Protocol-Version, it MUST respond with 400 Bad Request."
    assert r.status_code == 400
    assert ok.status_code == 200


async def test_accept_header_must_allow_json(engine, media):
    async with http_app(engine, media, MutableClock(NOW)) as http:
        r = await post(http, initialize(), {**HEADERS, "Accept": "text/html"})
    assert r.status_code == 406


async def test_batch_is_rejected(engine, media):
    """2025-06-18 removed JSON-RPC batching: "The body of the POST request MUST be a single
    JSON-RPC request, notification, or response"."""
    async with http_app(engine, media, MutableClock(NOW)) as http:
        r = await post(http, [rpc("ping", id=1), rpc("ping", id=2)])
    assert r.status_code == 400


def test_refuses_to_start_without_a_token(media):
    with pytest.raises(RuntimeError, match="MCP_TOKEN"):
        create_app(Settings(_env_file=None, mcp_token="", media_dir=media))
