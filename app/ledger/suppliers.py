"""Per-supplier reliability over recent business days, for the Suppliers page.

Computed from each order (expected delivery) and its reconciliation:
- due: orders already decided. An order still pending (window plus grace not over, nothing
  matched yet) is left out of every rate.
- delivered: matched, short or over.
- on time: delivered no later than the window end. Missing orders and late arrivals are not on
  time, so on-time % = on time / due.
- late: delivered after the window end. The matcher still pairs these (up to
  RECONCILE_MAX_OFFSET_HOURS outside the window), so they show up here rather than as missing.
- short-delivery rate = short / delivered; boxes short = sum of (expected - received) over
  short orders; missed = missing orders.

The worst supplier is the one with the highest share of due orders that had any problem
(missing, short or late), ties broken by boxes short, then by missed deliveries. These are
ledger figures for the business, not measurements of Dockhand: they appear on the dashboard
only, never in the README or a results file.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.models import deliveries, expected_deliveries, reconciliations
from app.ledger.queries import live_expected
from app.reconcile.store import day_bounds

DELIVERED = ("matched", "short", "over")

d, e, r = deliveries.c, expected_deliveries.c, reconciliations.c


def ratio(k: int, n: int) -> dict:
    return {"value": round(k / n, 4) if n else None, "k": k, "n": n}


@dataclass
class _Tally:
    orders: int = 0
    pending: int = 0
    due: int = 0
    delivered: int = 0
    on_time: int = 0
    late: int = 0
    short: int = 0
    over: int = 0
    missed: int = 0
    boxes_short: int = 0
    problems: int = 0
    simulated_orders: int = 0
    last_order: datetime | None = field(default=None)


def _score(t: _Tally) -> tuple[float, int, int]:
    return (t.problems / t.due, t.boxes_short, t.missed)


async def supplier_stats(
    conn: AsyncConnection, today: date, tz: ZoneInfo, days: int, include_simulated: bool = True
) -> dict:
    """Stats for orders whose window starts in the `days` business days ending today."""
    first = today - timedelta(days=days - 1)
    start, _ = day_bounds(first, tz)
    _, end = day_bounds(today, tz)
    stmt = (
        select(
            e.supplier,
            e.expected_count,
            e.window_start,
            e.window_end,
            e.simulated,
            r.status,
            d.package_count,
            d.detected_at,
        )
        .select_from(
            expected_deliveries.outerjoin(reconciliations, r.expected_id == e.id).outerjoin(
                deliveries, d.id == r.delivery_id
            )
        )
        .where(e.window_start >= start, e.window_start < end, live_expected(include_simulated))
    )
    tallies: dict[str, _Tally] = {}
    for row in (await conn.execute(stmt)).mappings():
        t = tallies.setdefault(row["supplier"].strip(), _Tally())
        t.orders += 1
        t.simulated_orders += row["simulated"]
        if t.last_order is None or row["window_start"] > t.last_order:
            t.last_order = row["window_start"]
        status = row["status"]
        if status is None:
            t.pending += 1
            continue
        t.due += 1
        problem = False
        if status == "missing":
            t.missed += 1
            problem = True
        elif status in DELIVERED:
            t.delivered += 1
            if row["detected_at"] <= row["window_end"]:
                t.on_time += 1
            else:
                t.late += 1
                problem = True
            if status == "short":
                t.short += 1
                t.boxes_short += row["expected_count"] - row["package_count"]
                problem = True
            elif status == "over":
                t.over += 1
        t.problems += problem

    ranked = [name for name, t in tallies.items() if t.due]
    worst = max(ranked, key=lambda n: (_score(tallies[n]), n), default=None)
    # No problems anywhere means there is no "worst" to call out.
    if worst is not None and tallies[worst].problems == 0:
        worst = None

    rows = [
        {
            "supplier": name,
            "orders": t.orders,
            "pending": t.pending,
            "due": t.due,
            "delivered": t.delivered,
            "on_time": ratio(t.on_time, t.due),
            "late": t.late,
            "short_rate": ratio(t.short, t.delivered),
            "over": t.over,
            "missed": t.missed,
            "boxes_short": t.boxes_short,
            "problem_rate": ratio(t.problems, t.due),
            "simulated_orders": t.simulated_orders,
            "last_order": t.last_order,
            "worst": name == worst,
        }
        for name, t in sorted(tallies.items(), key=lambda kv: kv[0].lower())
    ]
    n_sim = sum(t.simulated_orders for t in tallies.values())
    return {
        "from": first.isoformat(),
        "to": today.isoformat(),
        "days": days,
        "suppliers": rows,
        "worst": worst,
        "orders": sum(t.orders for t in tallies.values()),
        "simulated_orders": n_sim,
        "includes_simulated": n_sim > 0,
    }
