"""The assistant loop: scripted Bedrock responses, the real MCP server over Streamable HTTP and
a real ledger. Checks what the model is sent, what runs, and the confirmation gate on writes."""

import contextlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import func, select

from app.assistant.agent import (
    MAX_HISTORY,
    REPLY_FALLBACK,
    Approval,
    Assistant,
    TurnError,
    bedrock_schema,
    spoken,
    trim,
)
from app.config import Settings
from app.db.models import expected_deliveries
from app.main import create_app as create_api
from mcp_server.server import create_app as create_mcp
from tests.mcp_fixtures import AUTH, BASE, MutableClock, settings
from tests.pipeline_fakes import T0, add_expected
from tests.test_ledger_api import add_delivery, reconcile

NOW = T0 + timedelta(hours=3)  # Fri 2026-10-16, 13:00 in Los Angeles


def tool_use(name: str, args: dict, id: str = "t1", text: str | None = None) -> dict:
    content: list[dict] = [{"text": text}] if text else []
    content.append({"toolUse": {"toolUseId": id, "name": name, "input": args}})
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 900, "outputTokens": 40},
    }


def say(text: str) -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 1200, "outputTokens": 30},
    }


class FakeBedrock:
    def __init__(self, responses: list[dict]) -> None:
        self.responses, self.requests = list(responses), []

    def converse(self, **kwargs: Any) -> dict:
        # Deep copy: the agent keeps appending to the same message list.
        self.requests.append(json.loads(json.dumps(kwargs)))
        return self.responses.pop(0)


@pytest.fixture
def media(tmp_path) -> Path:
    p = tmp_path / "media"
    p.mkdir()
    return p


@contextlib.asynccontextmanager
async def assistant_for(engine, media, responses, **over):
    clock = MutableClock(NOW)
    app = create_mcp(settings(media), engine=engine, clock=clock)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)

        @contextlib.asynccontextmanager
        async def connect():
            async with httpx2.AsyncClient(transport=transport, base_url=BASE, headers=AUTH) as h:
                stream = streamable_http_client(f"{BASE}/mcp", http_client=h)
                async with Client(stream, mode="legacy") as client:
                    yield client

        bedrock = FakeBedrock(responses)
        cfg = Settings(_env_file=None, business_timezone="America/Los_Angeles", **over)
        yield Assistant(bedrock, connect, cfg, clock), bedrock


async def seed(engine, media):
    await add_expected(engine, supplier="Sysco", carrier="UPS", count=3)
    await add_delivery(engine, media, T0, count=1)
    await reconcile(engine, NOW)


async def n_orders(engine) -> int:
    async with engine.connect() as conn:
        stmt = select(func.count()).select_from(expected_deliveries)
        return (await conn.execute(stmt)).scalar_one()


def test_bedrock_schema_inlines_refs_and_drops_null_unions():
    schema = {
        "type": "object",
        "title": "addArguments",
        "properties": {
            "carrier": {"anyOf": [{"$ref": "#/$defs/Carrier"}, {"type": "null"}], "default": None},
            "date": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "Day"},
            "count": {"type": "integer", "title": "Count"},
        },
        "required": ["count"],
        "$defs": {"Carrier": {"enum": ["UPS", "Sysco"], "type": "string", "title": "Carrier"}},
    }
    assert bedrock_schema(schema) == {
        "type": "object",
        "properties": {
            "carrier": {"enum": ["UPS", "Sysco"], "type": "string"},
            "date": {"type": "string", "description": "Day"},
            "count": {"type": "integer"},
        },
        "required": ["count"],
    }


def test_spoken_strips_thinking_and_markdown():
    text = "<thinking>Call check_delivery.</thinking> **Yes**, Sysco came\n at 10:00 AM."
    assert spoken(text) == "Yes, Sysco came at 10:00 AM."


def test_trim_starts_at_a_user_message():
    turn = [
        {"role": "user", "content": [{"text": "q"}]},
        {"role": "assistant", "content": [{"toolUse": {}}]},
        {"role": "user", "content": [{"toolResult": {}}]},
        {"role": "assistant", "content": [{"text": "a"}]},
    ]
    history = turn * 10
    kept = trim(history)
    assert len(kept) <= MAX_HISTORY
    assert kept[0] == {"role": "user", "content": [{"text": "q"}]}
    assert trim(turn) == turn


async def test_question_runs_read_tool_and_answers(engine, media):
    await seed(engine, media)
    responses = [
        tool_use("check_delivery", {"supplier": "Sysco", "date": "today"}),
        say("<thinking>Short.</thinking> Sysco came at 10 AM but left 1 of 3 boxes."),
    ]
    async with assistant_for(engine, media, responses) as (assistant, bedrock):
        result = await assistant.turn([], text="Did the Sysco delivery come today?")

    assert result.reply == "Sysco came at 10 AM but left 1 of 3 boxes."
    assert result.pending is None
    first, second = bedrock.requests
    assert first["modelId"] == "us.amazon.nova-pro-v1:0"  # BEDROCK_MODEL_ID by default
    assert "Today is Friday, October 16, 2026" in first["system"][0]["text"]
    names = [t["toolSpec"]["name"] for t in first["toolConfig"]["tools"]]
    assert len(names) == 5 and "check_delivery" in names
    # The model's second call carries the MCP tool result.
    tool_result = second["messages"][-1]["content"][0]["toolResult"]
    assert tool_result["toolUseId"] == "t1" and tool_result["status"] == "success"
    data = json.loads(tool_result["content"][0]["text"])
    assert data["summary"] == "Sysco came at 10:00 AM today but left 1 of 3 boxes: 2 short."

    methods = [(t["method"], t.get("tool")) for t in result.trace]
    assert methods == [("tools/list", None), ("tools/call", "check_delivery")]
    call = result.trace[1]
    assert call["arguments"] == {"supplier": "Sysco", "date": "today"}
    assert call["is_error"] is False and "resource_link" in call["content_types"]
    (card,) = result.cards
    assert card["status"] == "short" and card["image_url"].startswith(f"{BASE}/media/")
    assert [m["role"] for m in result.messages] == ["user", "assistant", "user", "assistant"]
    assert result.usage == {"input_tokens": 2100, "output_tokens": 70}
    assert len(result.latency_ms["model_ms"]) == 2 and len(result.latency_ms["tool_ms"]) == 1


async def test_write_tool_waits_for_confirmation(engine, media):
    args = {
        "supplier": "Daybreak Bakery",
        "count": 4,
        "date": "tomorrow",
        "window_start": "07:00",
        "window_end": "09:00",
    }
    responses = [tool_use("add_expected_delivery", args, id="w1"), say("Done, it's on the list.")]
    async with assistant_for(engine, media, responses) as (assistant, bedrock):
        asked = await assistant.turn(
            [], text="Add an expected delivery from Daybreak Bakery, 4 boxes, tomorrow 7 to 9 AM"
        )
        assert asked.pending == {
            "tool_use_id": "w1",
            "tool": "add_expected_delivery",
            "arguments": args,
            "question": (
                "Add an expected delivery: Daybreak Bakery, 4 boxes, tomorrow, 7:00 AM to 9:00 AM?"
            ),
        }
        assert asked.reply == asked.pending["question"]
        assert await n_orders(engine) == 0  # nothing ran yet
        assert [t["method"] for t in asked.trace] == ["tools/list"]

        done = await assistant.turn(asked.messages, approval=Approval("w1", approved=True))
    assert done.reply == "Done, it's on the list."
    assert done.pending is None
    assert await n_orders(engine) == 1
    (call,) = [t for t in done.trace if t["method"] == "tools/call"]
    assert call["tool"] == "add_expected_delivery" and call["result"]["created"] is True
    assert done.cards[0]["title"] == "Daybreak Bakery · 4 boxes"
    sent = bedrock.requests[-1]["messages"][-1]["content"][0]["toolResult"]
    assert sent["status"] == "success"


async def test_declined_write_does_nothing(engine, media):
    args = {"supplier": "X", "count": 1, "date": "today", "window_start": "9", "window_end": "10"}
    responses = [tool_use("add_expected_delivery", args, id="w1"), say("Okay, I won't add it.")]
    async with assistant_for(engine, media, responses) as (assistant, bedrock):
        asked = await assistant.turn([], text="add X")
        done = await assistant.turn(asked.messages, approval=Approval("w1", approved=False))
    assert done.reply == "Okay, I won't add it."
    assert await n_orders(engine) == 0
    sent = bedrock.requests[-1]["messages"][-1]["content"][0]["toolResult"]
    assert sent["status"] == "error" and "declined" in sent["content"][0]["text"]
    assert done.trace[-1] == {
        "method": "tools/call",
        "tool": "add_expected_delivery",
        "arguments": args,
        "declined": True,
    }


async def test_approval_must_match_a_pending_write(engine, media):
    async with assistant_for(engine, media, []) as (assistant, _):
        with pytest.raises(TurnError):
            await assistant.turn([], approval=Approval("w1", approved=True))
        read_only = [
            {"role": "user", "content": [{"text": "q"}]},
            tool_use("check_delivery", {"supplier": "Sysco"}, id="r1")["output"]["message"],
        ]
        with pytest.raises(TurnError):
            await assistant.turn(read_only, approval=Approval("r1", approved=True))
    with pytest.raises(TurnError):
        await Assistant(None, None, Settings(_env_file=None), MutableClock(NOW)).turn([])


async def test_stops_after_max_steps(engine, media):
    loop = [tool_use("list_discrepancies", {}, id=f"t{i}") for i in range(3)]
    async with assistant_for(engine, media, loop, assistant_max_steps=3) as (assistant, bedrock):
        result = await assistant.turn([], text="loop forever")
    assert result.reply == REPLY_FALLBACK
    assert len(bedrock.requests) == 3


@contextlib.asynccontextmanager
async def api_client(engine, assistant=None, **over):
    cfg = Settings(_env_file=None, admin_token="admin", **over)
    app = create_api(cfg, engine=engine, start_background=False, assistant=assistant)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer admin"},
        ) as c:
            yield c


async def test_api_turn(engine, media):
    await seed(engine, media)
    responses = [tool_use("get_todays_deliveries", {}), say("One delivery so far today.")]
    async with assistant_for(engine, media, responses) as (assistant, _):
        async with api_client(engine, assistant) as c:
            r = await c.post("/v1/assistant/turn", json={"text": "What came today?"})
            both = await c.post(
                "/v1/assistant/turn",
                json={"text": "x", "approval": {"tool_use_id": "a", "approved": True}},
            )
            stale = await c.post(
                "/v1/assistant/turn", json={"approval": {"tool_use_id": "a", "approved": True}}
            )
            anon = await c.post(
                "/v1/assistant/turn", json={"text": "hi"}, headers={"Authorization": ""}
            )
    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == "One delivery so far today."
    assert [t["method"] for t in body["trace"]] == ["tools/list", "tools/call"]
    assert body["cards"] and body["model_id"]
    assert both.status_code == 422
    assert stale.status_code == 409
    assert anon.status_code == 401


async def test_api_needs_mcp_token(engine):
    async with api_client(engine) as c:
        r = await c.post("/v1/assistant/turn", json={"text": "hi"})
    assert r.status_code == 503
    assert "MCP_TOKEN" in r.json()["detail"]


async def test_api_reports_unreachable_mcp_server(engine):
    # Nothing listens on port 9 (discard); the connection is refused.
    async with api_client(engine, mcp_token="t", mcp_url="http://127.0.0.1:9/mcp") as c:
        r = await c.post("/v1/assistant/turn", json={"text": "hi"})
    assert r.status_code == 502
    assert "MCP server unreachable" in r.json()["detail"]
