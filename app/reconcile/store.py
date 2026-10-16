"""Load one business day from Postgres, run `reconcile()`, and replace that day's rows.

The matcher is a pure function, so persistence only has to make "replace the day's rows" atomic
and serial:
- One transaction deletes every reconciliation touching the day's deliveries or expected
  deliveries and inserts the new set. Row IDs are uuid5(expected_id|delivery_id), so the same
  inputs always produce the same rows: re-running is a no-op in effect.
- A transaction-scoped advisory lock serialises runs. Without it, two runs (a new delivery and
  the timer) could interleave their delete and insert and leave a stale row behind.

A day is a calendar day in BUSINESS_TIMEZONE. Each expected delivery belongs to the day its
window starts, each delivery to the day it was detected, so every item is in exactly one batch.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from sqlalchemy import delete, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import deliveries, expected_deliveries, reconciliations
from app.reconcile.cost import ReconcileConfig
from app.reconcile.matcher import reconcile
from app.reconcile.models import DetectedDelivery, ExpectedDelivery, ReconcileResult

log = logging.getLogger(__name__)

# Arbitrary constant naming "the reconciliation lock" for pg_advisory_xact_lock.
ADVISORY_LOCK_KEY = 0x646F636B  # "dock"


@dataclass(frozen=True)
class DayRun:
    day: date
    result: ReconcileResult
    elapsed_ms: float


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, dtime.min, tzinfo=tz)
    return start, datetime.combine(day + timedelta(days=1), dtime.min, tzinfo=tz)


def business_day(t: datetime, tz: ZoneInfo) -> date:
    return t.astimezone(tz).date()


async def reconcile_day(
    engine: AsyncEngine, day: date, tz: ZoneInfo, now: datetime, cfg: ReconcileConfig
) -> DayRun:
    t0 = time.perf_counter()
    start, end = day_bounds(day, tz)
    d, e, r = deliveries.c, expected_deliveries.c, reconciliations.c
    async with engine.begin() as conn:
        await conn.execute(text("select pg_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
        drows = (
            await conn.execute(
                select(d.id, d.detected_at, d.carrier, d.package_count).where(
                    d.detected_at >= start, d.detected_at < end
                )
            )
        ).mappings()
        ds = [DetectedDelivery(**row) for row in drows]
        erows = (
            await conn.execute(
                select(
                    e.id,
                    e.supplier,
                    e.carrier,
                    e.expected_count,
                    e.window_start,
                    e.window_end,
                    e.reference,
                ).where(e.window_start >= start, e.window_start < end)
            )
        ).mappings()
        es = [ExpectedDelivery(**row) for row in erows]

        result = reconcile(ds, es, now, cfg)

        d_ids = [x.id for x in ds]
        e_ids = [x.id for x in es]
        if d_ids or e_ids:
            await conn.execute(
                delete(reconciliations).where(
                    or_(r.delivery_id.in_(d_ids), r.expected_id.in_(e_ids))
                )
            )
        if result.reconciliations:
            await conn.execute(
                insert(reconciliations),
                [rec.model_dump(mode="python") for rec in result.reconciliations],
            )
    return DayRun(day=day, result=result, elapsed_ms=round((time.perf_counter() - t0) * 1000, 1))


async def reconcile_recent(
    engine: AsyncEngine,
    tz: ZoneInfo,
    now: datetime,
    cfg: ReconcileConfig,
) -> list[DayRun]:
    """Timer entry point: today and yesterday. Yesterday is included because a window that
    ends late in the evening only reaches window_end + grace after midnight."""
    today = business_day(now, tz)
    return [
        await reconcile_day(engine, day, tz, now, cfg) for day in (today - timedelta(days=1), today)
    ]


class ReconcileTimer:
    """Re-run reconciliation periodically so expected deliveries turn `missing` after their
    window plus grace, even when no new delivery arrives to trigger a run."""

    def __init__(
        self,
        engine: AsyncEngine,
        tz: ZoneInfo,
        cfg: ReconcileConfig,
        clock: Callable[[], datetime],
    ) -> None:
        self._engine, self._tz, self._cfg, self._clock = engine, tz, cfg, clock

    async def run(self, interval_s: float) -> None:
        while True:
            try:
                await reconcile_recent(self._engine, self._tz, self._clock(), self._cfg)
            except Exception:
                log.exception("scheduled reconciliation failed")
            await asyncio.sleep(interval_s)
