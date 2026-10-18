"""Record an expected delivery and reconcile its day: the one write path for orders.

Shared by `POST /v1/expected` (dashboard, seed script) and the MCP tool
`add_expected_delivery`, so both get the same idempotency rule: the caller chooses the row ID.
Repeating a request with the same ID and the same fields returns the stored row; the same ID
with different fields is a conflict, so a retried request never edits an order behind the
operator's back.
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import expected_deliveries, reconciliations
from app.reconcile.cost import ReconcileConfig
from app.reconcile.store import business_day, reconcile_day

FIELDS = ("supplier", "carrier", "expected_count", "window_start", "window_end", "reference")


class ExpectedConflict(ValueError):
    """The ID is taken by an order with different fields."""


@dataclass(frozen=True)
class AddResult:
    created: bool  # False: an identical order with this ID already existed
    expected: dict  # the stored row (id + FIELDS)
    # None while the window (+ grace) is still open and nothing has matched it.
    reconciliation: dict | None


async def add_expected(
    engine: AsyncEngine,
    values: dict,
    tz: ZoneInfo,
    now: datetime,
    cfg: ReconcileConfig,
) -> AddResult:
    """Insert `values` (id + FIELDS; carrier a plain string or None) and reconcile its day."""
    t = expected_deliveries
    async with engine.begin() as conn:
        inserted = (
            await conn.execute(
                insert(t).values(**values).on_conflict_do_nothing().returning(t.c.id)
            )
        ).scalar_one_or_none()
        row = (await conn.execute(select(t).where(t.c.id == values["id"]))).mappings().one()
    if inserted is None and any(row[k] != values[k] for k in FIELDS):
        raise ExpectedConflict(f"expected delivery {values['id']} exists with different fields")

    # Reconcile now, so a delivery that already arrived is matched without waiting for the
    # timer, and the caller can say where this order stands.
    await reconcile_day(engine, business_day(values["window_start"], tz), tz, now, cfg)
    r = reconciliations.c
    async with engine.connect() as conn:
        rec = (
            (await conn.execute(select(r.status, r.reason).where(r.expected_id == values["id"])))
            .mappings()
            .one_or_none()
        )
    return AddResult(
        created=inserted is not None,
        expected={k: row[k] for k in ("id", *FIELDS)},
        reconciliation=dict(rec) if rec else None,
    )
