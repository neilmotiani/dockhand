"""The simulated Alexa+ assistant: a Bedrock Converse tool-use loop over the Dockhand MCP server.

One `turn` takes the conversation so far plus either the user's words or their answer to a
confirmation, and runs model → tools → model until the model answers in words. Tools come from
the MCP server's `tools/list` and run through `tools/call`, over Streamable HTTP like any other
MCP client would use them; nothing here imports the tools directly.

Human in the loop: the MCP spec says a client SHOULD confirm operations with the user. Any tool
whose annotations don't mark it read-only (today: add_expected_delivery) pauses the turn. The
client gets `pending` (what the model wants to do, in words), the user confirms or declines,
and the next turn carries that answer. Every tool use in the paused model message runs only
then, because Converse expects all of one message's tool results together.

The conversation lives with the client (stateless API, like the MCP server). It is the
dashboard's own state: anyone who can send it could add an order through the dashboard form
anyway, so a forged history grants nothing new.
"""

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from mcp import Client

from app.config import Settings

ConnectMcp = Callable[[], AbstractAsyncContextManager[Client]]

MAX_HISTORY = 24  # messages kept from earlier turns
REPLY_FALLBACK = "Sorry, I couldn't work that out. Could you ask again?"
_THINKING = re.compile(r"<thinking>.*?</thinking>", re.DOTALL | re.IGNORECASE)
_MARKDOWN = re.compile(r"[*_`#]+")


class TurnError(ValueError):
    """The request does not fit the conversation (e.g. an approval with nothing pending)."""


@dataclass
class Approval:
    tool_use_id: str
    approved: bool


@dataclass
class TurnResult:
    reply: str
    messages: list[dict]
    cards: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    pending: dict | None = None
    latency_ms: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    model_id: str = ""

    def as_dict(self) -> dict:
        return {
            "reply": self.reply,
            "messages": self.messages,
            "cards": self.cards,
            "trace": self.trace,
            "pending": self.pending,
            "latency_ms": self.latency_ms,
            "usage": self.usage,
            "model_id": self.model_id,
        }


def bedrock_schema(schema: dict) -> dict:
    """An MCP inputSchema as Converse tool schema: `$ref`s inlined, `anyOf [X, null]` reduced
    to X (optional fields are already optional by not being `required`) and titles dropped.
    Some Bedrock models reject `$defs` and null unions."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = defs[node["$ref"].rsplit("/", 1)[-1]]
            merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return walk(merged)
        if "anyOf" in node:
            branches = [b for b in node["anyOf"] if b.get("type") != "null"]
            if len(branches) == 1:
                rest = {k: v for k, v in node.items() if k not in ("anyOf", "default")}
                return walk({**branches[0], **rest})
        return {k: walk(v) for k, v in node.items() if k not in ("title", "$defs")}

    return walk(schema)


def spoken(text: str) -> str:
    """Model text as something to say: no <thinking> blocks (Nova emits them around tool
    calls), no Markdown markers, single spaces."""
    text = _MARKDOWN.sub("", _THINKING.sub("", text))
    return re.sub(r"\s+", " ", text).strip()


def describe_action(name: str, args: dict) -> str:
    """The confirmation question for a write tool, built from the arguments themselves (not
    from model prose), so the user confirms exactly what will run."""
    if name == "add_expected_delivery":
        boxes = args.get("count")
        bits = [f"{args.get('supplier')}, {boxes} box{'es' if boxes != 1 else ''}"]
        if args.get("carrier"):
            bits.append(f"by {args['carrier']}")
        start, end = _clock(args.get("window_start")), _clock(args.get("window_end"))
        bits.append(f"{args.get('date')}, {start} to {end}")
        if args.get("reference"):
            bits.append(f"reference {args['reference']}")
        return f"Add an expected delivery: {', '.join(bits)}?"
    return f"Run {name} with {json.dumps(args)}?"


def _clock(value: Any) -> str:
    """'07:00' → '7:00 AM' for the question; anything else is shown as the model wrote it
    (the MCP tool validates it either way)."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value).strip())
    if not m or int(m.group(1)) > 23:
        return str(value)
    h = int(m.group(1))
    return f"{h % 12 or 12}:{m.group(2)} {'AM' if h < 12 else 'PM'}"


def trim(messages: list[dict]) -> list[dict]:
    """The last MAX_HISTORY messages, starting at a user message that carries the user's words
    (never at a tool result, which would orphan it from its tool use)."""
    if len(messages) <= MAX_HISTORY:
        return messages
    for i in range(len(messages) - MAX_HISTORY, len(messages)):
        m = messages[i]
        if m["role"] == "user" and any("text" in c for c in m["content"]):
            return messages[i:]
    return []


def _tool_uses(message: dict) -> list[dict]:
    return [c["toolUse"] for c in message.get("content", []) if "toolUse" in c]


def _text(message: dict) -> str:
    return " ".join(c["text"] for c in message.get("content", []) if "text" in c)


class Assistant:
    def __init__(
        self,
        bedrock: Any,
        connect: ConnectMcp,
        settings: Settings,
        clock: Callable[[], datetime],
    ) -> None:
        self._bedrock, self._connect, self._settings, self._clock = (
            bedrock,
            connect,
            settings,
            clock,
        )
        self.model_id = settings.assistant_model_id or settings.bedrock_model_id

    def system_prompt(self) -> str:
        s = self._settings
        tz = ZoneInfo(s.business_timezone)
        now = self._clock().astimezone(tz)
        business = f" for {s.business_name}" if s.business_name else ""
        return (
            f"You are Dockhand, the voice receiving assistant{business}. A Ring camera at the "
            "back door logs every delivery, and you answer questions about deliveries, "
            "expected orders and suppliers.\n"
            f"Today is {now:%A, %B} {now.day}, {now.year}; the time is {now:%H:%M} "
            f"({s.business_timezone}). Pass dates as YYYY-MM-DD or 'today'/'tomorrow'.\n"
            "Rules:\n"
            "- Use the tools for every fact. Never guess or invent deliveries, counts or times.\n"
            "- Your reply is spoken aloud: one to three short sentences, no lists, no Markdown, "
            "no IDs or URLs. The screen shows the details and snapshots.\n"
            "- Each tool result has a `summary` you can say almost as is.\n"
            "- To add an expected delivery, call add_expected_delivery straight away with what "
            "the user said. The app asks the user to confirm before it runs, so don't ask "
            "first. Leave out the carrier if the user didn't name one.\n"
            "- If a result has includes_simulated true, say briefly that it includes demo data.\n"
            "- If the user declines an action, acknowledge it in a few words."
        )

    async def turn(
        self,
        history: list[dict],
        text: str | None = None,
        approval: Approval | None = None,
    ) -> TurnResult:
        if (text is None) == (approval is None):
            raise TurnError("send either text or an approval")
        try:
            return await self._turn(history, text, approval)
        except BaseExceptionGroup as eg:
            # The MCP client's task groups wrap whatever escapes the connection. A single
            # error (TurnError, a Bedrock ClientError, ...) is re-raised as itself.
            errors = leaves(eg)
            if len(errors) == 1:
                raise errors[0] from None
            raise

    async def _turn(
        self, history: list[dict], text: str | None, approval: Approval | None
    ) -> TurnResult:
        t0 = time.perf_counter()
        messages = trim(list(history))
        trace: list[dict] = []
        cards: list[dict] = []
        latency: dict[str, Any] = {"model_ms": [], "tool_ms": []}
        usage = {"input_tokens": 0, "output_tokens": 0}

        async with self._connect() as mcp:
            t = time.perf_counter()
            listed = (await mcp.list_tools()).tools
            latency["tools_list_ms"] = round((time.perf_counter() - t) * 1000, 1)
            trace.append(
                {
                    "method": "tools/list",
                    "tools": [x.name for x in listed],
                    "duration_ms": latency["tools_list_ms"],
                }
            )
            writes = {
                x.name for x in listed if not (x.annotations and x.annotations.read_only_hint)
            }
            tool_config = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": x.name,
                            "description": x.description or x.name,
                            "inputSchema": {"json": bedrock_schema(x.input_schema)},
                        }
                    }
                    for x in listed
                ]
            }

            async def run_tools(uses: list[dict], approved: set[str]) -> list[dict]:
                results = []
                for use in uses:
                    if use["name"] in writes and use["toolUseId"] not in approved:
                        trace.append(
                            {
                                "method": "tools/call",
                                "tool": use["name"],
                                "arguments": use["input"],
                                "declined": True,
                            }
                        )
                        results.append(
                            {
                                "toolResult": {
                                    "toolUseId": use["toolUseId"],
                                    "content": [{"text": "The user declined. Nothing was done."}],
                                    "status": "error",
                                }
                            }
                        )
                        continue
                    results.append(await call_tool(use))
                return results

            async def call_tool(use: dict) -> dict:
                t = time.perf_counter()
                res = await mcp.call_tool(use["name"], use["input"])
                ms = round((time.perf_counter() - t) * 1000, 1)
                latency["tool_ms"].append(ms)
                data = res.structured_content
                texts = [c.text for c in res.content if c.type == "text"]
                trace.append(
                    {
                        "method": "tools/call",
                        "tool": use["name"],
                        "arguments": use["input"],
                        "is_error": bool(res.is_error),
                        "duration_ms": ms,
                        "summary": data.get("summary") if data else None,
                        "error": texts[0] if res.is_error and texts else None,
                        "result": data,
                        "content_types": [c.type for c in res.content],
                    }
                )
                if data and not res.is_error:
                    cards.extend(data.get("cards", []))
                # The model gets the JSON (summary included); the screen gets the cards.
                body = json.dumps(data) if data else "\n".join(texts)
                return {
                    "toolResult": {
                        "toolUseId": use["toolUseId"],
                        "content": [{"text": body}],
                        "status": "error" if res.is_error else "success",
                    }
                }

            if approval is not None:
                last = messages[-1] if messages else None
                uses = _tool_uses(last) if last and last["role"] == "assistant" else []
                target = next((u for u in uses if u["toolUseId"] == approval.tool_use_id), None)
                if target is None or target["name"] not in writes:
                    raise TurnError("nothing is waiting for that confirmation")
                approved = {target["toolUseId"]} if approval.approved else set()
                messages.append({"role": "user", "content": await run_tools(uses, approved)})
            else:
                messages.append({"role": "user", "content": [{"text": text}]})

            reply, pending = REPLY_FALLBACK, None
            for _ in range(self._settings.assistant_max_steps):
                t = time.perf_counter()
                resp = await asyncio.to_thread(
                    self._bedrock.converse,
                    modelId=self.model_id,
                    system=[{"text": self.system_prompt()}],
                    messages=messages,
                    toolConfig=tool_config,
                    inferenceConfig={
                        "maxTokens": self._settings.assistant_max_tokens,
                        "temperature": 0.2,
                    },
                )
                latency["model_ms"].append(round((time.perf_counter() - t) * 1000, 1))
                for k, v in (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens")):
                    usage[k] += resp.get("usage", {}).get(v, 0)
                message = resp["output"]["message"]
                messages.append(message)
                uses = _tool_uses(message)
                if resp.get("stopReason") != "tool_use" or not uses:
                    reply = spoken(_text(message)) or REPLY_FALLBACK
                    break
                write = next((u for u in uses if u["name"] in writes), None)
                if write is not None:
                    question = describe_action(write["name"], write["input"])
                    pending = {
                        "tool_use_id": write["toolUseId"],
                        "tool": write["name"],
                        "arguments": write["input"],
                        "question": question,
                    }
                    reply = question
                    break
                messages.append({"role": "user", "content": await run_tools(uses, set())})

        latency["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return TurnResult(
            reply=reply,
            messages=messages,
            cards=_dedupe(cards),
            trace=trace,
            pending=pending,
            latency_ms=latency,
            usage=usage,
            model_id=self.model_id,
        )


def leaves(e: BaseException) -> list[BaseException]:
    if isinstance(e, BaseExceptionGroup):
        return [x for sub in e.exceptions for x in leaves(sub)]
    return [e]


def _dedupe(cards: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in cards:
        key = (c.get("title"), c.get("subtitle"), c.get("image_url"))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def mcp_connector(settings: Settings) -> ConnectMcp:
    """Connect to MCP_URL with the bearer token, negotiating 2025-11-25 (initialize handshake).
    A new connection per turn: the server is stateless, so there is nothing to keep open."""
    import contextlib

    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    from mcp_types import Implementation

    if settings.mcp_token is None:
        raise RuntimeError("MCP_TOKEN is not set")
    headers = {"Authorization": f"Bearer {settings.mcp_token.get_secret_value()}"}

    @contextlib.asynccontextmanager
    async def connect() -> AsyncIterator[Client]:
        async with httpx2.AsyncClient(headers=headers, timeout=30) as http:
            transport = streamable_http_client(settings.mcp_url, http_client=http)
            info = Implementation(name="dockhand-assistant", version="0.1.0")
            async with Client(transport, mode="legacy", client_info=info) as client:
                yield client

    return connect
