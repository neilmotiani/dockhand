"""Every MCP tool, called through the SDK client over Streamable HTTP against a real ledger."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, insert, select

from app.db.models import expected_deliveries
from app.reconcile.cost import ReconcileConfig
from app.reconcile.store import business_day, reconcile_day
from mcp_server.tools import resolve_supplier
from tests.mcp_fixtures import MutableClock, mcp_client
from tests.pipeline_fakes import T0, add_expected
from tests.test_ledger_api import LA, add_delivery, reconcile

NOW = T0 + timedelta(hours=3)  # 13:00 in Los Angeles on Fri 2026-10-16


@pytest.fixture
def media(tmp_path) -> Path:
    p = tmp_path / "media"
    p.mkdir()
    return p


def structured(result) -> dict:
    assert not result.is_error, result.content
    return result.structured_content


async def seed_day(engine, media):
    """Sysco short (1 of 3), a stray DHL drop, US Foods still due later today."""
    await add_expected(engine, supplier="Sysco", carrier="UPS", count=3)
    await add_expected(
        engine,
        supplier="US Foods",
        carrier=None,
        count=2,
        start=T0 + timedelta(hours=4),
        end=T0 + timedelta(hours=6),
    )
    short = await add_delivery(engine, media, T0, count=1)
    stray = await add_delivery(engine, media, T0 + timedelta(minutes=30), count=4, carrier="DHL")
    await reconcile(engine, NOW)
    return short, stray


async def test_lists_five_tools_with_schemas_and_annotations(engine, media):
    async with mcp_client(engine, media, MutableClock(NOW)) as (client, _):
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "get_todays_deliveries",
        "check_delivery",
        "list_discrepancies",
        "add_expected_delivery",
        "get_supplier_scorecard",
    }
    for name, t in tools.items():
        assert t.input_schema["type"] == "object"
        assert t.output_schema and t.output_schema["type"] == "object"
        assert "summary" in t.output_schema["properties"]
        writes = name == "add_expected_delivery"
        assert t.annotations.read_only_hint is not writes
    add = tools["add_expected_delivery"]
    assert add.annotations.idempotent_hint is True
    assert add.annotations.destructive_hint is False
    assert set(add.input_schema["required"]) == {
        "supplier",
        "count",
        "date",
        "window_start",
        "window_end",
    }


async def test_todays_deliveries_with_statuses_and_signed_snapshots(engine, media):
    short, stray = await seed_day(engine, media)
    clock = MutableClock(NOW)
    async with mcp_client(engine, media, clock) as (client, http):
        res = await client.call_tool("get_todays_deliveries", {})
        out = structured(res)
        assert out["date"] == "2026-10-16"
        by_id = {d["id"]: d for d in out["deliveries"]}
        assert by_id[str(short)]["status"] == "short"
        assert by_id[str(short)]["supplier"] == "Sysco"
        assert by_id[str(short)]["time"] == "10:00 AM"
        assert by_id[str(stray)]["status"] == "unexpected"
        assert [o["status"] for o in out["orders"]] == ["short", "pending"]
        assert out["summary"].startswith("2 deliveries logged today: Sysco, 1 box at 10:00 AM")
        assert "Still expected: US Foods (2:00 PM to 4:00 PM)." in out["summary"]
        assert out["includes_simulated"] is False

        # Text content: the summary, then the same JSON as structuredContent, then one
        # resource link per snapshot.
        assert res.content[0].text == out["summary"]
        assert json.loads(res.content[1].text) == out
        links = [c for c in res.content if c.type == "resource_link"]
        assert len(links) == 2 and all(c.mime_type == "image/jpeg" for c in links)

        url = by_id[str(short)]["snapshot_url"]
        assert url.startswith(f"http://test/media/deliveries/{short}/snapshot?")
        # Loads with no Authorization header: the signature is the permission.
        img = await http.get(url)
        assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
        assert img.content.startswith(b"\xff\xd8")
        q = parse_qs(urlsplit(url).query)
        bad = url.replace(q["sig"][0], "0" * 64)
        assert (await http.get(bad)).status_code == 403
        other = url.replace(str(short), str(stray))  # a valid signature for another file
        assert (await http.get(other)).status_code == 403
        clock.now = NOW + timedelta(hours=2)  # past MCP_MEDIA_TTL_S (1 h)
        assert (await http.get(url)).status_code == 403


async def test_todays_deliveries_other_day_and_bad_date(engine, media):
    await seed_day(engine, media)
    async with mcp_client(engine, media, MutableClock(NOW)) as (client, _):
        out = structured(await client.call_tool("get_todays_deliveries", {"date": "tomorrow"}))
        assert out["date"] == "2026-10-17"
        assert out["deliveries"] == [] and out["orders"] == []
        assert out["summary"] == "No deliveries logged tomorrow. No orders were expected tomorrow."
        bad = await client.call_tool("get_todays_deliveries", {"date": "next tuesday"})
        assert bad.is_error
        assert "YYYY-MM-DD" in bad.content[0].text


async def test_check_delivery_by_spoken_supplier_name(engine, media):
    await seed_day(engine, media)
    async with mcp_client(engine, media, MutableClock(NOW)) as (client, _):
        # Speech recognition often writes "Cisco" for Sysco.
        out = structured(
            await client.call_tool("check_delivery", {"supplier": "the Cisco delivery"})
        )
    assert out["found"] is True and out["matched_supplier"] == "Sysco"
    (order,) = out["orders"]
    assert order["status"] == "short"
    assert (order["expected_count"], order["delivered_count"]) == (3, 1)
    assert out["summary"] == "Sysco came at 10:00 AM today but left 1 of 3 boxes: 2 short."
    (card,) = out["cards"]
    assert card["status"] == "short" and card["image_url"].startswith("http://test/media/")


async def test_check_delivery_pending_missing_and_reference(engine, media):
    await seed_day(engine, media)
    eid = await add_expected(
        engine,
        supplier="Daybreak Bakery",
        carrier=None,
        count=2,
        start=T0 - timedelta(hours=4),  # 6:00 to 7:00 AM: past the grace hour by 13:00
        end=T0 - timedelta(hours=3),
    )
    async with engine.begin() as conn:
        await conn.execute(
            expected_deliveries.update()
            .where(expected_deliveries.c.id == eid)
            .values(reference="PO-77")
        )
    await reconcile(engine, NOW)
    async with mcp_client(engine, media, MutableClock(NOW)) as (client, _):
        pending = structured(await client.call_tool("check_delivery", {"supplier": "us foods"}))
        missing = structured(await client.call_tool("check_delivery", {"reference": "po-77"}))
        none = structured(await client.call_tool("check_delivery", {"supplier": "Zebra Farms"}))
        empty = await client.call_tool("check_delivery", {})
    assert pending["orders"][0]["status"] == "pending"
    assert pending["summary"].startswith("US Foods is due today, 2:00 PM to 4:00 PM (2 boxes).")
    assert missing["orders"][0]["status"] == "missing"
    assert missing["summary"].startswith("Daybreak Bakery (PO-77) has not arrived")
    assert none["found"] is False and none["matched_supplier"] is None
    assert none["summary"] == "I don't know a supplier called Zebra Farms."
    assert empty.is_error and "supplier name or an order reference" in empty.content[0].text


async def test_check_delivery_finds_unexpected_drop_by_carrier(engine, media):
    """No order from DHL, but a DHL delivery came: say so instead of 'no order found'."""
    await add_delivery(engine, media, T0, count=2, carrier="DHL")
    await reconcile(engine, NOW)
    async with mcp_client(engine, media, MutableClock(NOW)) as (client, _):
        out = structured(
            await client.call_tool("check_delivery", {"supplier": "DHL", "date": "today"})
        )
    assert out["found"] is False
    assert out["summary"].startswith("I found no order from DHL today.")
    (stray,) = out["unexpected_deliveries"]
    assert stray["status"] == "unexpected" and stray["package_count"] == 2
    assert "An unexpected DHL delivery of 2 boxes was logged at 10:00 AM." in out["summary"]


async def test_list_discrepancies_day_and_range(engine, media):
    await seed_day(engine, media)
    async with mcp_client(engine, media, MutableClock(NOW)) as (client, _):
        today = structured(await client.call_tool("list_discrepancies", {}))
        week = structured(
            await client.call_tool(
                "list_discrepancies", {"start_date": "2026-10-10", "end_date": "2026-10-16"}
            )
        )
        quiet = structured(
            await client.call_tool("list_discrepancies", {"start_date": "yesterday"})
        )
        backwards = await client.call_tool(
            "list_discrepancies", {"start_date": "2026-10-16", "end_date": "2026-10-01"}
        )
        too_long = await client.call_tool(
            "list_discrepancies", {"start_date": "2026-08-01", "end_date": "2026-10-16"}
        )
    assert today["counts"] == {"short": 1, "unexpected": 1}
    assert [x["status"] for x in today["discrepancies"]] == ["short", "unexpected"]
    assert today["summary"].startswith("2 problems today: 1 short and 1 unexpected. Short by 2")
    assert week["total"] == 2 and week["start_date"] == "2026-10-10"
    assert quiet["total"] == 0 and quiet["summary"].startswith("No discrepancies yesterday.")
    assert backwards.is_error and too_long.is_error
    assert "31 days" in too_long.content[0].text


async def count_orders(engine) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(select(func.count()).select_from(expected_deliveries))
        ).scalar_one()


async def test_add_expected_delivery_is_idempotent(engine, media):
    args = {
        "supplier": "Daybreak Bakery",
        "count": 4,
        "date": "tomorrow",
        "window_start": "7 AM",
        "window_end": "09:00",
    }
    async with mcp_client(engine, media, MutableClock(NOW)) as (client, _):
        first = structured(await client.call_tool("add_expected_delivery", args))
        again = structured(await client.call_tool("add_expected_delivery", args))
        listed = structured(await client.call_tool("get_todays_deliveries", {"date": "2026-10-17"}))
    assert first["created"] is True and again["created"] is False
    assert first["order"]["id"] == again["order"]["id"]
    assert await count_orders(engine) == 1
    order = first["order"]
    # 7 AM in Los Angeles (PDT) is 14:00 UTC.
    assert datetime.fromisoformat(order["window_start"]) == datetime(
        2026, 10, 17, 14, 0, tzinfo=UTC
    )
    assert datetime.fromisoformat(order["window_end"]) == datetime(2026, 10, 17, 16, 0, tzinfo=UTC)
    assert order["carrier"] is None and order["status"] == "pending"
    assert first["summary"] == ("Added: Daybreak Bakery, 4 boxes, tomorrow, 7:00 AM to 9:00 AM.")
    assert again["summary"].startswith("Already on the list: Daybreak Bakery")
    assert [o["supplier"] for o in listed["orders"]] == ["Daybreak Bakery"]


async def test_add_expected_delivery_matches_an_arrival_and_validates(engine, media):
    await add_delivery(engine, media, T0, count=5, carrier="Sysco")
    await reconcile(engine, NOW)
    async with mcp_client(engine, media, MutableClock(NOW)) as (client, _):
        out = structured(
            await client.call_tool(
                "add_expected_delivery",
                {
                    "supplier": "Sysco",
                    "carrier": "Sysco",
                    "count": 5,
                    "date": "2026-10-16",
                    "window_start": "9:00",
                    "window_end": "11:00",
                    "reference": "PO-5",
                },
            )
        )
        bad = {
            "past": {"date": "yesterday"},
            "time": {"window_start": "breakfast"},
            "order": {"window_start": "11:00", "window_end": "9:00"},
        }
        errors = {}
        for k, over in bad.items():
            base = {
                "supplier": "X",
                "count": 1,
                "date": "today",
                "window_start": "9:00",
                "window_end": "10:00",
            }
            errors[k] = await client.call_tool("add_expected_delivery", {**base, **over})
        schema = await client.call_tool(
            "add_expected_delivery",
            {"supplier": "X", "count": 0, "date": "today", "window_start": "9", "window_end": "10"},
        )
    # Reconciled on insert: the delivery already logged at 10:00 now matches.
    assert out["order"]["status"] == "matched"
    assert out["summary"].endswith("Sysco (PO-5) delivered 5 boxes at 10:00 AM today, as expected.")
    assert all(r.is_error for r in errors.values())
    assert "in the past" in errors["past"].content[0].text
    assert "07:00" in errors["time"].content[0].text
    assert "after window_start" in errors["order"].content[0].text
    assert schema.is_error  # count >= 1 is enforced by the input schema
    assert await count_orders(engine) == 1


async def add_order(engine, supplier, day_offset, count, carrier=None, simulated=False):
    at = T0 + timedelta(days=day_offset)
    async with engine.begin() as conn:
        await conn.execute(
            insert(expected_deliveries).values(
                id=uuid.uuid4(),
                supplier=supplier,
                carrier=carrier,
                expected_count=count,
                window_start=at - timedelta(hours=1),
                window_end=at + timedelta(hours=1),
                simulated=simulated,
            )
        )


async def test_supplier_scorecard(engine, media):
    # Three days ago: Sysco short by 2, US Foods and Daybreak complete. Yesterday: Sysco and
    # Daybreak complete, US Foods never came.
    for offset in (-3, -1):
        await add_order(engine, "Sysco", offset, 3, carrier="Sysco")
        await add_order(engine, "US Foods", offset, 2, carrier="US Foods")
        await add_order(engine, "Daybreak Bakery", offset, 1, carrier="Other")
    arrivals = [(-3, "Sysco", 1), (-3, "US Foods", 2), (-3, "Other", 1)]
    arrivals += [(-1, "Sysco", 3), (-1, "Other", 1)]
    for i, (offset, carrier, count) in enumerate(arrivals):
        at = T0 + timedelta(days=offset, minutes=i)
        await add_delivery(engine, media, at, count=count, carrier=carrier)
    for offset in (-3, -1):
        day = business_day(T0 + timedelta(days=offset), LA)
        await reconcile_day(engine, day, LA, NOW, ReconcileConfig())

    async with mcp_client(engine, media, MutableClock(NOW)) as (client, _):
        out = structured(await client.call_tool("get_supplier_scorecard", {}))
        one = structured(await client.call_tool("get_supplier_scorecard", {"supplier": "sisco"}))
        unknown = await client.call_tool("get_supplier_scorecard", {"supplier": "Zebra"})
    rows = {r["supplier"]: r for r in out["suppliers"]}
    assert {
        k: (r["orders_due"], r["short"], r["boxes_short"], r["missed"]) for k, r in rows.items()
    } == {
        "Sysco": (2, 1, 2, 0),
        "US Foods": (2, 0, 0, 1),
        "Daybreak Bakery": (2, 0, 0, 0),
    }
    assert out["most_boxes_short"] == "Sysco"
    # Sysco and US Foods both had a problem on 1 of 2 orders; boxes short breaks the tie.
    assert out["worst"] == "Sysco"
    assert out["summary"] == (
        "Sysco shorts you the most: 1 short delivery, 2 boxes missing in the last 30 days."
    )
    assert [r["supplier"] for r in out["suppliers"]][0] == "Sysco"
    assert out["includes_simulated"] is False
    assert [r["supplier"] for r in one["suppliers"]] == ["Sysco"]
    assert one["summary"] == (
        "Over the last 30 days, Sysco: 2 of 2 orders on time, 1 short (2 boxes in all), 0 missed."
    )
    assert unknown.is_error and "known suppliers" in unknown.content[0].text


async def test_simulated_history_is_labelled(engine, media):
    await add_order(engine, "Sysco", 0, 3, carrier="Sysco", simulated=True)
    await reconcile(engine, NOW)
    async with mcp_client(engine, media, MutableClock(NOW)) as (client, _):
        out = structured(await client.call_tool("check_delivery", {"supplier": "Sysco"}))
        hidden = structured(await client.call_tool("get_supplier_scorecard", {}))
    assert out["includes_simulated"] is True
    assert out["cards"][0]["simulated"] is True
    assert out["summary"].endswith(
        "Note: this includes simulated demo history, not real deliveries."
    )
    assert hidden["includes_simulated"] is True

    async with mcp_client(engine, media, MutableClock(NOW), mcp_include_simulated=False) as (
        client,
        _,
    ):
        live = structured(await client.call_tool("check_delivery", {"supplier": "Sysco"}))
    assert live["found"] is False and live["includes_simulated"] is False


async def test_rate_limit_is_a_tool_error(engine, media):
    async with mcp_client(engine, media, MutableClock(NOW), mcp_rate_limit_per_min=2) as (
        client,
        _,
    ):
        results = [await client.call_tool("list_discrepancies", {}) for _ in range(3)]
    assert [r.is_error for r in results] == [False, False, True]
    assert "Too many tool calls" in results[2].content[0].text


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Sysco", "Sysco"),
        ("cisco", "Sysco"),
        ("the sysco delivery", "Sysco"),
        ("day break", "Daybreak Bakery"),
        ("Daybreak", "Daybreak Bakery"),
        ("U.S. Foods", "US Foods"),
        ("ridgeline", "Ridgeline Roasters"),
        ("Amazon", "Amazon Business"),
        ("Zebra Farms", None),
        ("delivery", None),
    ],
)
def test_resolve_supplier(query, expected):
    names = ["Amazon Business", "Daybreak Bakery", "Ridgeline Roasters", "Sysco", "US Foods"]
    assert resolve_supplier(query, names) == expected


async def test_check_delivery_on_a_day_without_an_order_names_the_last_and_next(engine, media):
    await add_order(engine, "Sysco", -2, 6, carrier="Sysco")
    await add_delivery(engine, media, T0 + timedelta(days=-2), count=6, carrier="Sysco")
    await add_order(engine, "Sysco", 3, 5, carrier="Sysco")
    for offset in (-2, 3):
        day = business_day(T0 + timedelta(days=offset), LA)
        await reconcile_day(engine, day, LA, NOW, ReconcileConfig())
    async with mcp_client(engine, media, MutableClock(NOW)) as (client, _):
        out = structured(
            await client.call_tool("check_delivery", {"supplier": "Sysco", "date": "today"})
        )
    assert out["found"] is False and out["orders"] == []
    assert [o["status"] for o in out["nearby"]] == ["matched", "pending"]
    assert out["summary"] == (
        "I found no order from Sysco today. "
        "Most recent: Sysco delivered 6 boxes at 10:00 AM on Wednesday, October 14, as expected. "
        "Next: Sysco is due on Monday, October 19, 9:00 AM to 11:00 AM (5 boxes)."
    )
    assert len(out["cards"]) == 2
