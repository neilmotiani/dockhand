"""Simulated demo history: the seed script, how it is reconciled, shown, hidden and reset."""

import json
import uuid
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text, update

from app.db.models import deliveries, expected_deliveries, reconciliations
from app.db.session import init_db
from app.reconcile.cost import ReconcileConfig
from scripts.seed_demo_history import (
    DEMO_SUPPLIERS,
    SNAPSHOT_NAME,
    Composite,
    load_composites,
    pick_composite,
    seed_history,
    simulate_days,
)
from tests.pipeline_fakes import T0, add_expected, tiny_jpeg
from tests.test_ledger_api import DAY, LA, add_delivery, client_for, reconcile
from tests.test_reset_day import client_for as reset_client_for

TODAY = date(2026, 10, 17)
NOW = datetime(2026, 10, 17, 19, 0, tzinfo=UTC)  # noon in Los Angeles


@pytest.fixture
def media(tmp_path) -> Path:
    p = tmp_path / "media"
    p.mkdir()
    return p


@pytest.fixture
def composites(tmp_path) -> list[Composite]:
    root = tmp_path / "data"
    (root / "eval/synthetic").mkdir(parents=True)
    items = []
    for i, (carrier, n) in enumerate(
        [("Sysco", 4), ("US Foods", 3), ("Unknown", 1), ("FedEx", 2), ("Amazon", 2), ("UPS", 5)]
    ):
        rel = f"eval/synthetic/syn_{i:03d}.jpg"
        (root / rel).write_bytes(tiny_jpeg(i))
        items.append({"media": [rel], "carrier": carrier, "package_count": n})
    items_file = tmp_path / "items.jsonl"
    items_file.write_text("\n".join(json.dumps(x) for x in items) + "\n")
    return load_composites(items_file, root)


async def seed(engine, media, composites=None, **kw):
    return await seed_history(
        engine,
        today=TODAY,
        tz=LA,
        media_dir=media,
        now=NOW,
        cfg=ReconcileConfig(),
        composites=composites,
        **kw,
    )


def _is_snapshot(path: str) -> bool:
    return Path(path).is_file() and Path(path).name == SNAPSHOT_NAME


async def rows(engine, table, *cols):
    async with engine.connect() as conn:
        return (await conn.execute(select(*cols).select_from(table))).all()


# --- schema --------------------------------------------------------------------------------


async def test_init_db_upgrades_a_database_from_before_simulated_rows(engine, media):
    async with engine.begin() as conn:
        await conn.execute(text("alter table expected_deliveries drop column simulated"))
        await conn.execute(text("alter table deliveries drop constraint deliveries_source"))
        await conn.execute(
            text(
                "alter table deliveries add constraint deliveries_source"
                " check (source in ('ring_playground', 'ring_device'))"
            )
        )
    await init_db(engine)
    await init_db(engine)  # idempotent
    res = await seed(engine, media)
    assert res.deliveries > 0


# --- generation ----------------------------------------------------------------------------


def test_history_has_named_suppliers_and_a_realistic_problem_rate():
    days = simulate_days(TODAY - timedelta(days=21), 21, 2027)
    suppliers = {x.supplier for d in days for x in d.expected}
    assert suppliers == {s.name for s in DEMO_SUPPLIERS}
    assert {"Sysco", "US Foods", "Amazon Business"} <= suppliers
    statuses = Counter(str(t.status) for d in days for t in d.truth)
    late = sum("late" in t.tags for d in days for t in d.truth)
    total = sum(statuses.values())
    # Most deliveries match; a handful of each kind of problem.
    assert statuses["matched"] / total > 0.75
    assert statuses["short"] >= 1 and statuses["missing"] >= 1 and statuses["unexpected"] >= 1
    assert late >= 1


def test_a_date_simulates_the_same_whichever_day_the_script_runs():
    a = simulate_days(date(2026, 9, 26), 21, 2027)
    b = simulate_days(date(2026, 9, 27), 21, 2027)  # run one day later
    by_day = {d.day: d for d in a}
    for d in b[:-1]:
        assert d.expected == by_day[d.day].expected
        assert d.deliveries == by_day[d.day].deliveries


def test_composite_choice_prefers_carrier_then_count(composites):
    did = uuid.uuid4()
    assert pick_composite(composites, did, "Sysco", 9).carrier == "Sysco"
    assert pick_composite(composites, did, "Other", 2).carrier == "Unknown"  # bakery van
    # No DHL composite: nearest count from any carrier.
    assert pick_composite(composites, did, "DHL", 5).count == 5
    assert pick_composite([], did, "UPS", 1) is None


# --- seeding -------------------------------------------------------------------------------


async def test_seed_writes_only_simulated_rows_and_reconciles_every_day(engine, media, composites):
    res = await seed(engine, media, composites)
    assert res.days == 21 and res.expected > 30 and res.deliveries > 30
    srcs = {s for (s,) in await rows(engine, deliveries, deliveries.c.source)}
    assert srcs == {"simulated"}
    assert all(
        sim for (sim,) in await rows(engine, expected_deliveries, expected_deliveries.c.simulated)
    )
    # Days run from three weeks ago to yesterday; nothing today.
    first, last = (
        await rows(
            engine,
            deliveries,
            func.min(deliveries.c.detected_at),
            func.max(deliveries.c.detected_at),
        )
    )[0]
    assert first.astimezone(LA).date() >= TODAY - timedelta(days=21)
    assert last.astimezone(LA).date() < TODAY
    # Every order and every delivery has a decision (all windows are over).
    n_recs = len(await rows(engine, reconciliations, reconciliations.c.id))
    statuses = Counter(s for (s,) in await rows(engine, reconciliations, reconciliations.c.status))
    assert n_recs == res.expected + statuses["unexpected"]
    assert sum(res.statuses.values()) == n_recs
    # Each delivery has its composite snapshot on disk.
    paths = [p for (p,) in await rows(engine, deliveries, deliveries.c.snapshot_path)]
    assert res.snapshots == res.deliveries == len(paths)
    assert all(_is_snapshot(p) for p in paths)


async def test_seed_is_safe_to_rerun(engine, media, composites):
    await seed(engine, media, composites)
    before = {
        t.name: sorted(await rows(engine, t, *t.c))
        for t in (deliveries, expected_deliveries, reconciliations)
    }
    again = await seed(engine, media, composites)
    after = {
        t.name: sorted(await rows(engine, t, *t.c))
        for t in (deliveries, expected_deliveries, reconciliations)
    }
    assert again.replaced_deliveries == len(before["deliveries"])
    # Same rows, same IDs; only decided_at/created_at may move.
    for name in before:
        assert [r[0] for r in after[name]] == [r[0] for r in before[name]]
    assert len(list(media.iterdir())) == again.deliveries


async def test_rerun_never_touches_real_rows(engine, media):
    sysco = await add_expected(engine, supplier="Sysco", carrier=None, count=1)
    live = await add_delivery(engine, media, T0, carrier="Unknown")
    await reconcile(engine, T0 + timedelta(hours=1))
    await seed(engine, media)
    await seed(engine, media)
    async with engine.connect() as conn:
        rec = (
            (
                await conn.execute(
                    select(reconciliations.c.expected_id, reconciliations.c.status).where(
                        reconciliations.c.delivery_id == live
                    )
                )
            )
            .mappings()
            .one()
        )
    assert rec == {"expected_id": sysco, "status": "matched"}
    assert (media / str(live)).is_dir()


async def test_simulated_and_real_rows_are_never_paired(engine, media):
    """A simulated order and a real delivery on the same day, a perfect pair on every other
    count, stay apart: the real delivery is unexpected, the simulated order missing."""
    order = await add_expected(engine, supplier="Sysco", carrier="UPS", count=1)
    async with engine.begin() as conn:
        await conn.execute(
            update(expected_deliveries)
            .where(expected_deliveries.c.id == order)
            .values(simulated=True)
        )
    live = await add_delivery(engine, media, T0, count=1, carrier="UPS")
    await reconcile(engine, T0 + timedelta(hours=3))
    got = {
        (e, d): s
        for e, d, s in await rows(
            engine,
            reconciliations,
            reconciliations.c.expected_id,
            reconciliations.c.delivery_id,
            reconciliations.c.status,
        )
    }
    assert got == {(order, None): "missing", (None, live): "unexpected"}


# --- API: badge flag and toggle ------------------------------------------------------------


async def test_rows_are_flagged_and_can_be_hidden(engine, media, composites):
    await seed(engine, media, composites)
    await add_delivery(engine, media, NOW - timedelta(hours=1))  # a live one, today
    async with engine.connect() as conn:
        (day,) = (
            await conn.execute(
                select(deliveries.c.detected_at)
                .where(deliveries.c.source == "simulated")
                .order_by(deliveries.c.detected_at)
                .limit(1)
            )
        ).one()
    sim_day = day.astimezone(LA).date().isoformat()
    async with client_for(engine, media) as c:
        shown = (await c.get("/v1/deliveries", params={"date": sim_day})).json()["deliveries"]
        hidden = (
            await c.get("/v1/deliveries", params={"date": sim_day, "include_simulated": "false"})
        ).json()["deliveries"]
        exp = (await c.get("/v1/expected", params={"date": sim_day})).json()["expected"]
        exp_hidden = (
            await c.get("/v1/expected", params={"date": sim_day, "include_simulated": "false"})
        ).json()["expected"]
        snap = await c.get(shown[0]["media"]["snapshot"])
    assert shown and all(x["simulated"] and x["source"] == "simulated" for x in shown)
    assert hidden == [] and exp_hidden == []
    assert exp and all(x["simulated"] for x in exp)
    assert snap.status_code == 200 and snap.headers["content-type"] == "image/jpeg"


async def test_discrepancies_toggle(engine, media):
    await seed(engine, media)
    days = {
        r[0].astimezone(LA).date()
        for r in await rows(engine, expected_deliveries, expected_deliveries.c.window_start)
    }
    seen = []
    async with client_for(engine, media) as c:
        for d in sorted(days):
            on = (await c.get("/v1/discrepancies", params={"date": d.isoformat()})).json()
            off = (
                await c.get(
                    "/v1/discrepancies",
                    params={"date": d.isoformat(), "include_simulated": "false"},
                )
            ).json()
            assert off["discrepancies"] == []
            seen += on["discrepancies"]
    assert seen and all(x["simulated"] for x in seen)
    assert {x["status"] for x in seen} >= {"short", "missing", "unexpected"}


# --- suppliers -----------------------------------------------------------------------------


@pytest.fixture
def api_now(monkeypatch):
    """Pin the API's clock to the afternoon of DAY (16:00 in Los Angeles), so "the last 30
    days" is fixed."""
    at = T0 + timedelta(hours=6)
    monkeypatch.setattr("app.ledger.api.utcnow", lambda: at)
    return at


async def _order(engine, supplier, count, start, end, simulated=False):
    eid = await add_expected(
        engine, supplier=supplier, carrier=None, count=count, start=start, end=end
    )
    if simulated:
        async with engine.begin() as conn:
            await conn.execute(
                update(expected_deliveries)
                .where(expected_deliveries.c.id == eid)
                .values(simulated=True)
            )
    return eid


async def test_supplier_stats(engine, media, api_now):
    w0, w1 = T0 - timedelta(hours=1), T0 + timedelta(hours=1)
    # Acme: one on time, one late (+2 h), one missing. Bolt: one short by 2.
    await _order(engine, "Acme", 1, w0, w1)
    await _order(engine, "Acme", 1, w0 + timedelta(hours=3), w1 + timedelta(hours=3))
    await _order(engine, "Acme", 5, w0 + timedelta(hours=6), w1 + timedelta(hours=6))
    await _order(engine, "Bolt", 3, w0 + timedelta(hours=9), w1 + timedelta(hours=9))
    await add_delivery(engine, media, T0, count=1, carrier="Unknown")
    await add_delivery(engine, media, w1 + timedelta(hours=5), count=1, carrier="Unknown")
    await add_delivery(engine, media, T0 + timedelta(hours=9), count=1, carrier="Unknown")
    await reconcile(engine, T0 + timedelta(hours=14))
    # Not reconciled yet, so it counts as an order but not in any rate.
    await _order(engine, "Bolt", 1, T0 + timedelta(hours=5), T0 + timedelta(hours=6))

    async with client_for(engine, media) as c:
        body = (await c.get("/v1/suppliers")).json()
    by = {x["supplier"]: x for x in body["suppliers"]}
    acme, bolt = by["Acme"], by["Bolt"]
    assert body["date"] == body["today"] and body["days"] == 30
    assert acme["due"] == 3 and acme["missed"] == 1 and acme["late"] == 1
    assert acme["on_time"] == {"value": 0.3333, "k": 1, "n": 3}
    assert acme["short_rate"] == {"value": 0.0, "k": 0, "n": 2}
    assert bolt["orders"] == 2 and bolt["pending"] == 1 and bolt["due"] == 1
    assert bolt["short_rate"] == {"value": 1.0, "k": 1, "n": 1} and bolt["boxes_short"] == 2
    # Bolt: 1 of 1 due had a problem; Acme: 2 of 3.
    assert body["worst"] == "Bolt" and bolt["worst"] and not acme["worst"]
    assert body["includes_simulated"] is False


async def test_supplier_stats_say_when_simulated_data_is_included(engine, media, api_now):
    w0, w1 = T0 - timedelta(hours=1), T0 + timedelta(hours=1)
    await _order(engine, "Acme", 1, w0, w1, simulated=True)
    await _order(engine, "Acme", 1, w0, w1)
    await reconcile(engine, T0 + timedelta(hours=3))
    async with client_for(engine, media) as c:
        on = (await c.get("/v1/suppliers")).json()
        off = (await c.get("/v1/suppliers", params={"include_simulated": "false"})).json()
    assert on["includes_simulated"] is True and on["simulated_orders"] == 1
    assert on["suppliers"][0]["missed"] == 2
    assert off["includes_simulated"] is False and off["suppliers"][0]["missed"] == 1


async def test_supplier_window_is_the_last_30_days(engine, media, api_now):
    old = T0 - timedelta(days=30)
    await _order(engine, "Old", 1, old, old + timedelta(hours=1))
    new = T0 - timedelta(days=29)
    await _order(engine, "New", 1, new, new + timedelta(hours=1))
    async with client_for(engine, media) as c:
        body = (await c.get("/v1/suppliers")).json()
        week = (await c.get("/v1/suppliers", params={"days": 7})).json()
    assert body["from"] == "2026-09-17" and body["to"] == DAY
    assert [x["supplier"] for x in body["suppliers"]] == ["New"]
    assert week["suppliers"] == []


# --- reset ---------------------------------------------------------------------------------


async def test_reset_keeps_simulated_history(engine, media, composites):
    await seed(engine, media, composites)
    n_sim = len(await rows(engine, deliveries, deliveries.c.id))
    n_sim_exp = len(await rows(engine, expected_deliveries, expected_deliveries.c.id))
    sim_recs = sorted(await rows(engine, reconciliations, reconciliations.c.id))
    # A live day on top: one order, one Playground delivery.
    await add_expected(engine, supplier="Sysco", carrier=None, count=1)
    await add_delivery(engine, media, T0)
    await reconcile(engine, T0 + timedelta(hours=1))
    # Reset the live day, then a day that only holds simulated history.
    sim_day = (TODAY - timedelta(days=2)).isoformat()
    async with reset_client_for(engine, media) as c:
        live = (await c.post("/v1/admin/reset-day", json={"date": DAY, "dry_run": False})).json()
        hist = (
            await c.post("/v1/admin/reset-day", json={"date": sim_day, "dry_run": False})
        ).json()
    assert live["deliveries"] == 1 and live["expected"] == 1
    assert hist["deliveries"] == 0 and hist["expected"] == 0 and hist["reconciliations"] == 0
    assert hist["kept_simulated"] > 0 and hist["kept_device_deliveries"] == 0
    assert len(await rows(engine, deliveries, deliveries.c.id)) == n_sim
    assert len(await rows(engine, expected_deliveries, expected_deliveries.c.id)) == n_sim_exp
    assert sorted(await rows(engine, reconciliations, reconciliations.c.id)) == sim_recs
    assert len(list(media.iterdir())) == n_sim
