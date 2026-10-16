import asyncio
import uuid
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select

from app.db.models import deliveries, reconciliations, ring_events
from app.reconcile.cost import ReconcileConfig
from app.reconcile.store import business_day, day_bounds, reconcile_day, reconcile_recent
from tests.pipeline_fakes import T0, add_expected

LA = ZoneInfo("America/Los_Angeles")
CFG = ReconcileConfig()


async def add_delivery(engine, at, count=2, carrier="UPS") -> uuid.UUID:
    ev, d = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            insert(ring_events).values(
                id=ev,
                request_id=f"r-{ev}",
                origin="manual",
                event_type="manual_trigger",
                received_at=at,
                raw={},
                relevant=True,
                status="done",
            )
        )
        await conn.execute(
            insert(deliveries).values(
                id=d,
                event_id=ev,
                detected_at=at,
                carrier=carrier,
                package_count=count,
                label_text=[],
                latency_ms={},
                source="own_footage",
            )
        )
    return d


async def stored(engine):
    async with engine.connect() as conn:
        rows = (await conn.execute(select(reconciliations))).mappings().all()
    return sorted((r["id"], r["status"], r["expected_id"], r["delivery_id"]) for r in rows)


def test_business_day_uses_local_midnight():
    # 06:30 UTC on the 16th is 23:30 on the 15th in Los Angeles.
    assert business_day(T0.replace(hour=6, minute=30), LA) == date(2026, 10, 15)
    start, end = day_bounds(date(2026, 10, 16), LA)
    assert (end - start) == timedelta(hours=24) and start.utcoffset() == timedelta(hours=-7)


async def test_rerun_is_idempotent_and_updates_on_new_input(engine):
    e1 = await add_expected(engine, supplier="Sysco", count=2)
    e2 = await add_expected(
        engine,
        supplier="US Foods",
        carrier="Other",
        count=1,
        start=T0 + timedelta(hours=2),
        end=T0 + timedelta(hours=3),
    )
    d1 = await add_delivery(engine, T0, count=2)
    day = business_day(T0, LA)

    await reconcile_day(engine, day, LA, T0 + timedelta(minutes=5), CFG)
    first = await stored(engine)
    assert [(s, e, d) for _, s, e, d in first] == [("matched", e1, d1)]  # e2 still pending

    await reconcile_day(engine, day, LA, T0 + timedelta(minutes=5), CFG)
    assert await stored(engine) == first

    # Past e2's window + grace: it turns missing; d1's row is unchanged.
    later = T0 + timedelta(hours=4, minutes=1)
    await reconcile_day(engine, day, LA, later, CFG)
    statuses = {(s, e, d) for _, s, e, d in await stored(engine)}
    assert statuses == {("matched", e1, d1), ("missing", e2, None)}

    # A new delivery that fits e2 replaces the missing row instead of adding beside it.
    d2 = await add_delivery(engine, T0 + timedelta(hours=2, minutes=30), count=1, carrier="Other")
    await reconcile_day(engine, day, LA, later, CFG)
    statuses = {(s, e, d) for _, s, e, d in await stored(engine)}
    assert statuses == {("matched", e1, d1), ("matched", e2, d2)}


async def test_unexpected_delivery_without_orders(engine):
    d = await add_delivery(engine, T0)
    await reconcile_day(engine, business_day(T0, LA), LA, T0, CFG)
    assert [(s, dd) for _, s, _, dd in await stored(engine)] == [("unexpected", d)]


async def test_other_days_are_untouched(engine):
    await add_delivery(engine, T0 - timedelta(days=2))
    await reconcile_day(engine, business_day(T0, LA), LA, T0, CFG)
    assert await stored(engine) == []


async def test_concurrent_runs_leave_one_consistent_set(engine):
    await add_expected(engine, count=2)
    for i in range(3):
        await add_delivery(engine, T0 + timedelta(minutes=i), count=2)
    day = business_day(T0, LA)
    await asyncio.gather(*(reconcile_day(engine, day, LA, T0, CFG) for _ in range(8)))
    rows = await stored(engine)
    assert len(rows) == 3  # 1 matched + 2 unexpected, no duplicates from interleaving
    assert sorted(s for _, s, _, _ in rows) == ["matched", "unexpected", "unexpected"]


async def test_reconcile_recent_covers_yesterday(engine):
    # Window ends 23:30 local on the 15th; grace runs past midnight.
    end = T0.replace(hour=6, minute=30)  # 23:30 LA on the 15th
    e = await add_expected(engine, start=end - timedelta(hours=1), end=end)
    runs = await reconcile_recent(engine, LA, end + timedelta(hours=2), CFG)
    assert [r.day for r in runs] == [date(2026, 10, 15), date(2026, 10, 16)]
    assert [(s, ee) for _, s, ee, _ in await stored(engine)] == [("missing", e)]
