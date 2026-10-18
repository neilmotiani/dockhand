"""Reset one business day of demo data, so a demo can be recorded again from a clean ledger.

Deletes, for the day: Playground deliveries and their evidence files, every real expected
delivery (orders belong to the day their window starts), the reconciliations touching either,
and the day's pipeline events. Deliveries from a real Ring device (`source = ring_device`) are
never deleted; they, and the events they came from, are kept and reconciled again. Simulated
history (scripts/seed_demo_history.py: `source = simulated` deliveries and `simulated` orders)
is kept too. It is reconciled in its own batch, so deleting real rows leaves its decisions
unchanged.

Runs in one transaction under the reconciliation advisory lock, so it can't interleave with a
reconciliation run. It refuses while an event of the day is still queued or being processed:
deleting an event under a running capture would leave the capture writing to a missing row.
"""

import asyncio
import logging
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, func, not_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import SIMULATED_SOURCE
from app.db.models import deliveries, expected_deliveries, reconciliations, ring_events
from app.reconcile.cost import ReconcileConfig
from app.reconcile.store import ADVISORY_LOCK_KEY, day_bounds, reconcile_day

log = logging.getLogger(__name__)

RESETTABLE_SOURCES = ("ring_playground",)


class ResetBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class ResetResult:
    date: str
    dry_run: bool
    deliveries: int
    expected: int
    reconciliations: int
    events: int
    media_dirs: int
    kept_device_deliveries: int
    kept_simulated: int  # simulated deliveries plus simulated orders of the day

    def as_dict(self) -> dict:
        return asdict(self)


def _remove_media(media_dir: Path, ids: list[uuid.UUID], dry_run: bool) -> int:
    """Delete each delivery's evidence directory; return how many there were."""
    n = 0
    root = media_dir.resolve()
    for did in ids:
        path = (media_dir / str(did)).resolve()
        # Only ever a direct child of MEDIA_DIR named by a delivery ID.
        if path.parent == root and path.is_dir():
            n += 1
            if not dry_run:
                shutil.rmtree(path)
    return n


async def reset_day(
    engine: AsyncEngine,
    day: date,
    tz: ZoneInfo,
    media_dir: Path,
    now: datetime,
    cfg: ReconcileConfig,
    *,
    dry_run: bool,
) -> ResetResult:
    start, end = day_bounds(day, tz)
    d, e, r, ev = deliveries.c, expected_deliveries.c, reconciliations.c, ring_events.c
    in_day = and_(d.detected_at >= start, d.detected_at < end)
    async with engine.begin() as conn:
        await conn.execute(text("select pg_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY})

        busy = (
            await conn.execute(
                select(func.count()).where(
                    ev.received_at >= start,
                    ev.received_at < end,
                    ev.status.in_(("pending", "processing")),
                )
            )
        ).scalar_one()
        if busy:
            raise ResetBlocked(f"{busy} event(s) still queued or processing; try again shortly")

        d_ids: list[uuid.UUID] = list(
            (
                await conn.execute(select(d.id).where(in_day, d.source.in_(RESETTABLE_SOURCES)))
            ).scalars()
        )
        kept = (
            await conn.execute(
                select(func.count()).where(
                    in_day,
                    not_(d.source.in_(RESETTABLE_SOURCES)),
                    d.source != SIMULATED_SOURCE,
                )
            )
        ).scalar_one()
        e_day = and_(e.window_start >= start, e.window_start < end)
        e_ids: list[uuid.UUID] = list(
            (await conn.execute(select(e.id).where(e_day, e.simulated.is_(False)))).scalars()
        )
        kept_sim = (
            await conn.execute(select(func.count()).where(in_day, d.source == SIMULATED_SOURCE))
        ).scalar_one() + (
            await conn.execute(select(func.count()).where(e_day, e.simulated.is_(True)))
        ).scalar_one()
        touching = or_(r.delivery_id.in_(d_ids), r.expected_id.in_(e_ids))
        n_recs = (await conn.execute(select(func.count()).where(touching))).scalar_one()
        # Events of the day, except those a kept (device) delivery still points at.
        kept_events = select(d.event_id).where(d.event_id.is_not(None), d.id.not_in(d_ids))
        day_events = and_(ev.received_at >= start, ev.received_at < end, ev.id.not_in(kept_events))
        n_events = (await conn.execute(select(func.count()).where(day_events))).scalar_one()

        if not dry_run:
            await conn.execute(delete(reconciliations).where(touching))
            if d_ids:
                await conn.execute(delete(deliveries).where(d.id.in_(d_ids)))
            await conn.execute(delete(ring_events).where(day_events))
            if e_ids:
                await conn.execute(delete(expected_deliveries).where(e.id.in_(e_ids)))

    media_dirs = await asyncio.to_thread(_remove_media, media_dir, d_ids, dry_run)

    if not dry_run and kept:
        await reconcile_day(engine, day, tz, now, cfg)

    result = ResetResult(
        date=day.isoformat(),
        dry_run=dry_run,
        deliveries=len(d_ids),
        expected=len(e_ids),
        reconciliations=n_recs,
        events=n_events,
        media_dirs=media_dirs,
        kept_device_deliveries=kept,
        kept_simulated=kept_sim,
    )
    log.info("reset day %s: %s", day, result)
    return result
