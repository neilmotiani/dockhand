"""Read side of the ledger: one business day of deliveries, expected deliveries, discrepancies
and pipeline activity, already joined with their reconciliation.

Plain functions over a connection, so the dashboard API and the MCP server share them. Every
timestamp is returned as an aware datetime; callers serialize. A delivery or expected delivery
with no reconciliation row yet is `pending` (an expected delivery before window_end + grace, or
a delivery in the instant between its insert and its reconciliation run).

Every row says whether it is simulated demo history (scripts/seed_demo_history.py), and the
day queries take `include_simulated` so the dashboard can hide it.
"""

import re
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, false, or_, select, true
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import SIMULATED_SOURCE
from app.db.models import deliveries, expected_deliveries, reconciliations, ring_events
from app.reconcile.models import DISCREPANCY_STATUSES
from app.reconcile.store import day_bounds

PENDING = "pending"
# Most urgent first: the order the discrepancy view lists them in.
DISCREPANCY_ORDER = ("missing", "short", "unexpected", "over")
assert set(DISCREPANCY_ORDER) == {s.value for s in DISCREPANCY_STATUSES}

# File names written by app/pipeline.py `_save_frames`: frame_<rank>_i<index>_t<seconds>s.jpg
SNAPSHOT_RANK = re.compile(r"^frame_(\d+)_")

d, e, r, ev = deliveries.c, expected_deliveries.c, reconciliations.c, ring_events.c


def media_urls(delivery_id: uuid.UUID, row: dict) -> dict[str, Any]:
    """API paths for a delivery's evidence. Frames are addressed by rank (0 = best scoring at
    capture time), never by file name, so a request cannot name an arbitrary file."""
    base = f"/v1/deliveries/{delivery_id}"
    # The snapshot is the best-scoring frame, which isn't always rank 0; its file name
    # (frame_<rank>_...) says which rank it is, so a client can skip it among the frames.
    m = SNAPSHOT_RANK.match(Path(row["snapshot_path"]).name) if row["snapshot_path"] else None
    return {
        "snapshot": f"{base}/snapshot" if row["snapshot_path"] else None,
        "snapshot_frame": int(m.group(1)) if m else None,
        "frames": [f"{base}/frames/{i}" for i in range(row["frames_sent"] or 0)]
        if row["snapshot_path"]
        else [],
        "clip": f"{base}/clip" if row["clip_path"] else None,
    }


def _delivery(row: dict) -> dict[str, Any]:
    latency = row["latency_ms"] or {}
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "device_id": row["device_id"],
        "detected_at": row["detected_at"],
        "carrier": row["carrier"],
        "package_count": row["package_count"],
        "label_text": list(row["label_text"] or []),
        "confidence": row["confidence"],
        "source": row["source"],
        "simulated": row["source"] == SIMULATED_SOURCE,
        "model_id": row["model_id"],
        "prompt_version": row["prompt_version"],
        "frames_sampled": row["frames_sampled"],
        "frames_sent": row["frames_sent"],
        "event_to_ledger_ms": latency.get("event_to_ledger"),
        "media": media_urls(row["id"], row),
    }


def _expected(row: dict) -> dict[str, Any]:
    return {
        "id": row["id"],
        "supplier": row["supplier"],
        "carrier": row["carrier"],
        "expected_count": row["expected_count"],
        "window_start": row["window_start"],
        "window_end": row["window_end"],
        "reference": row["reference"],
        "simulated": row["simulated"],
    }


DELIVERY_COLS = (
    d.id,
    d.event_id,
    d.device_id,
    d.detected_at,
    d.carrier,
    d.package_count,
    d.label_text,
    d.confidence,
    d.source,
    d.model_id,
    d.prompt_version,
    d.frames_sampled,
    d.frames_sent,
    d.latency_ms,
    d.snapshot_path,
    d.clip_path,
)
EXPECTED_COLS = (
    e.id,
    e.supplier,
    e.carrier,
    e.expected_count,
    e.window_start,
    e.window_end,
    e.reference,
    e.simulated,
)
REC_COLS = (r.status, r.reason, r.score, r.decided_at)


def _labelled(cols, prefix: str) -> list:
    return [c.label(f"{prefix}{c.name}") for c in cols]


def _split(row: dict, prefix: str) -> dict:
    return {k[len(prefix) :]: v for k, v in row.items() if k.startswith(prefix)}


def live_delivery(include_simulated: bool):
    """Filter on the deliveries side of a join. A missing side (outer join) passes."""
    if include_simulated:
        return true()
    return or_(d.id.is_(None), d.source != SIMULATED_SOURCE)


def live_expected(include_simulated: bool):
    if include_simulated:
        return true()
    return or_(e.id.is_(None), e.simulated.is_(false()))


async def day_deliveries(
    conn: AsyncConnection, day: date, tz: ZoneInfo, include_simulated: bool = True
) -> list[dict]:
    """The day's deliveries, newest first, each with its status and the order it matched."""
    start, end = day_bounds(day, tz)
    stmt = (
        select(*_labelled(DELIVERY_COLS, "d_"), *_labelled(EXPECTED_COLS, "e_"), *REC_COLS)
        .select_from(
            deliveries.outerjoin(reconciliations, r.delivery_id == d.id).outerjoin(
                expected_deliveries, e.id == r.expected_id
            )
        )
        .where(d.detected_at >= start, d.detected_at < end, live_delivery(include_simulated))
        .order_by(d.detected_at.desc(), d.id)
    )
    out = []
    for row in (await conn.execute(stmt)).mappings():
        exp = _split(row, "e_")
        out.append(
            {
                **_delivery(_split(row, "d_")),
                "status": row["status"] or PENDING,
                "reason": row["reason"],
                "expected": _expected(exp) if exp["id"] else None,
            }
        )
    return out


async def day_expected(
    conn: AsyncConnection, day: date, tz: ZoneInfo, include_simulated: bool = True
) -> list[dict]:
    """The day's expected deliveries in window order, each with its status and the delivery
    that filled it (if any)."""
    start, end = day_bounds(day, tz)
    stmt = (
        select(*_labelled(EXPECTED_COLS, "e_"), *_labelled(DELIVERY_COLS, "d_"), *REC_COLS)
        .select_from(
            expected_deliveries.outerjoin(reconciliations, r.expected_id == e.id).outerjoin(
                deliveries, d.id == r.delivery_id
            )
        )
        .where(e.window_start >= start, e.window_start < end, live_expected(include_simulated))
        .order_by(e.window_start, e.supplier, e.id)
    )
    out = []
    for row in (await conn.execute(stmt)).mappings():
        dl = _split(row, "d_")
        out.append(
            {
                **_expected(_split(row, "e_")),
                "status": row["status"] or PENDING,
                "reason": row["reason"],
                "delivery": _delivery(dl) if dl["id"] else None,
            }
        )
    return out


async def day_discrepancies(
    conn: AsyncConnection, day: date, tz: ZoneInfo, include_simulated: bool = True
) -> list[dict]:
    """Short, over, missing and unexpected decisions touching the day, most urgent first.

    A row belongs to the day if either side does: same rule as reconcile_day, which batches a
    delivery by its detection time and an order by its window start.
    """
    start, end = day_bounds(day, tz)
    stmt = (
        select(
            r.id,
            *REC_COLS,
            *_labelled(DELIVERY_COLS, "d_"),
            *_labelled(EXPECTED_COLS, "e_"),
        )
        .select_from(
            reconciliations.outerjoin(deliveries, d.id == r.delivery_id).outerjoin(
                expected_deliveries, e.id == r.expected_id
            )
        )
        .where(r.status.in_(DISCREPANCY_ORDER))
        .where(live_delivery(include_simulated), live_expected(include_simulated))
        .where(
            or_(
                and_(d.detected_at >= start, d.detected_at < end),
                and_(e.window_start >= start, e.window_start < end),
            )
        )
    )
    rows = (await conn.execute(stmt)).mappings().all()
    out = []
    for row in rows:
        dl, exp = _split(row, "d_"), _split(row, "e_")
        out.append(
            {
                "id": row["id"],
                # Both sides are simulated or neither: store.py reconciles them apart.
                "simulated": bool(exp["simulated"]) or dl["source"] == SIMULATED_SOURCE,
                "status": row["status"],
                "reason": row["reason"],
                "decided_at": row["decided_at"],
                "delivery": _delivery(dl) if dl["id"] else None,
                "expected": _expected(exp) if exp["id"] else None,
            }
        )

    def when(x: dict) -> datetime:
        return x["delivery"]["detected_at"] if x["delivery"] else x["expected"]["window_start"]

    out.sort(key=lambda x: (DISCREPANCY_ORDER.index(x["status"]), when(x), str(x["id"])))
    return out


async def recent_activity(
    conn: AsyncConnection, start: datetime, end: datetime, limit: int
) -> list[dict]:
    """Pipeline-relevant events received in [start, end), newest first, with the queue status
    and outcome. Lets the dashboard show an event in flight before its delivery row exists,
    and say why one produced no delivery (no packages, stale, Ring auth)."""
    stmt = (
        select(
            ev.id,
            ev.origin,
            ev.device_id,
            ev.event_type,
            ev.sub_type,
            ev.occurred_at,
            ev.received_at,
            ev.status,
            ev.attempts,
            ev.finished_at,
            ev.outcome,
            ev.last_error,
            d.id.label("delivery_id"),
        )
        .select_from(ring_events.outerjoin(deliveries, d.event_id == ev.id))
        .where(ev.relevant.is_(True), ev.received_at >= start, ev.received_at < end)
        .order_by(ev.received_at.desc(), ev.id)
        .limit(limit)
    )
    return [dict(row) for row in (await conn.execute(stmt)).mappings()]


async def delivery_media_paths(conn: AsyncConnection, delivery_id: uuid.UUID) -> dict | None:
    stmt = select(d.snapshot_path, d.clip_path, d.frames_sent).where(d.id == delivery_id)
    row = (await conn.execute(stmt)).mappings().one_or_none()
    return dict(row) if row else None
