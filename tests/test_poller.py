import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.db.models import ring_events
from app.ingest.poller import HistoryPoller
from app.ring.client import RingAuthError, RingClient

FIXTURES = Path(__file__).parent / "fixtures" / "ring"
NOW = datetime(2026, 10, 16, 17, 6, 30, tzinfo=UTC)  # fixture events are at 17:05 and 17:06


class StaticToken:
    def __init__(self) -> None:
        self.invalidated = False

    async def token(self) -> str:
        return "tok"

    async def invalidate(self) -> None:
        self.invalidated = True


def ring_with(history: dict, calls: list[httpx.Request], status: int = 200) -> RingClient:
    devices = json.loads((FIXTURES / "devices_list.json").read_text())["response"]["body"]

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        assert req.headers["authorization"] == "Bearer tok"
        if status != 200:
            return httpx.Response(status, json={"errors": []})
        if req.url.path == "/v1/devices":
            return httpx.Response(200, json=devices)
        assert req.url.path.startswith("/v1/history/devices/")
        return httpx.Response(200, json=history)

    http = httpx.AsyncClient(
        base_url="https://api.amazonvision.com", transport=httpx.MockTransport(handler)
    )
    return RingClient(http, StaticToken())


def history() -> dict:
    return json.loads((FIXTURES / "docs" / "history_events.json").read_text())["response"]


def poller(ring, engine, lookback=timedelta(minutes=5)) -> HistoryPoller:
    return HistoryPoller(
        ring,
        engine,
        event_types="motion.human,ding",
        relevant_rules=["motion_detected.human", "button_press"],
        lookback=lookback,
        clock=lambda: NOW,
    )


async def stored(engine):
    async with engine.connect() as conn:
        stmt = select(ring_events).order_by(ring_events.c.occurred_at)
        return (await conn.execute(stmt)).mappings().all()


async def test_poll_enqueues_new_events_once(engine):
    calls: list[httpx.Request] = []
    p = poller(ring_with(history(), calls), engine)
    notified = []
    p._on_insert = lambda: notified.append(1)
    assert await p.poll_once() == 2
    assert await p.poll_once() == 0
    assert notified == [1]
    rows = await stored(engine)
    assert [(r["event_type"], r["sub_type"], r["status"]) for r in rows] == [
        ("motion_detected", "human", "pending"),
        ("button_press", None, "pending"),
    ]
    assert all(r["origin"] == "poll" for r in rows)
    # Devices are listed once and cached; history is fetched with the configured filter.
    paths = [c.url.path for c in calls]
    assert paths.count("/v1/devices") == 1
    assert calls[1].url.params["event_types"] == "motion.human,ding"


async def test_poll_skips_events_before_lookback(engine):
    p = poller(ring_with(history(), []), engine, lookback=timedelta(seconds=60))
    # Only the 17:06:00 ding is within 60 s of 17:06:30.
    assert await p.poll_once() == 1
    assert [r["event_type"] for r in await stored(engine)] == ["button_press"]


async def test_poll_dedupes_against_webhook_with_same_event_id(engine):
    from app.ingest.events import from_webhook
    from app.ingest.queue import insert_event

    hook = json.loads((FIXTURES / "docs" / "webhook_motion_human.json").read_text())["payload"]
    hook["data"]["id"] = "hist-motion-1"
    async with engine.begin() as conn:
        await insert_event(conn, from_webhook(hook), True, NOW)
    assert await poller(ring_with(history(), []), engine).poll_once() == 1


async def test_empty_history_page(engine):
    empty = {"data": [], "links": {"next": "/v1/history/devices/x/events?page[key]=y"}}
    assert await poller(ring_with(empty, []), engine).poll_once() == 0


async def test_expired_token_raises_auth_error_and_invalidates(engine):
    ring = ring_with(history(), [], status=401)
    with pytest.raises(RingAuthError):
        await poller(ring, engine).poll_once()
    assert ring._tokens.invalidated


def recorded(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())["response"]["body"]


def test_recorded_history_shape():
    """Real Playground responses (2026-10-16): the filter was ignored, and items carry fields
    the docs don't list (relationships.cv_detections, meta.riid). links.next was absent."""
    filtered = recorded("history_events_filter_motion_human_ding")
    unfiltered = recorded("history_events_unfiltered")
    assert filtered["data"] == unfiltered["data"]
    assert {i["attributes"]["event_type"] for i in filtered["data"]} == {"on_demand"}
    assert "links" not in filtered
    item = filtered["data"][0]
    assert set(item["attributes"]) >= {"event_type", "start", "end", "is_third_party_reviewed"}
    assert item["relationships"]["source"]["data"]["type"] == "devices"


async def test_recorded_playground_history_enqueues_nothing(engine):
    """Our own live views come back as on_demand; none may trigger a capture, and none are
    stored."""
    page = recorded("history_events_filter_motion_human_ding")
    p = poller(ring_with(page, []), engine, lookback=timedelta(days=3650))
    assert await p.poll_once() == 0
    assert await stored(engine) == []


async def test_ignored_filter_means_no_inferred_subtype(engine):
    """If the page contains a type we didn't request, the server ignored the filter, so a
    motion item must not be labelled `human`."""
    page = recorded("history_events_filter_motion_human_ding")
    motion = json.loads(json.dumps(history()["data"][1]))  # docs-shaped motion item
    page = {"data": [motion, *page["data"][:2]]}
    p = poller(ring_with(page, []), engine, lookback=timedelta(days=3650))
    assert await p.poll_once() == 0  # motion with unknown subtype doesn't meet motion.human

    p = poller(ring_with(page, []), engine, lookback=timedelta(days=3650))
    p._rules = ["motion_detected"]  # an operator who accepts any motion
    assert await p.poll_once() == 1
    (row,) = await stored(engine)
    assert row["event_type"] == "motion_detected" and row["sub_type"] is None
