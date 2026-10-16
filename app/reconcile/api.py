"""POST /v1/expected: record a delivery the business expects, then reconcile its day.

Minimal operator endpoint (admin token) until the dashboard's expected-list editor exists.
Idempotent: the client may send its own `id`. Repeating a request with the same id and the
same fields returns the stored row; the same id with different fields is a 409, so a retried
request never edits an order behind the operator's back.
"""

import uuid
from typing import Self
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.auth import require_admin
from app.db.models import expected_deliveries, reconciliations
from app.extract.schemas import Carrier
from app.ingest.queue import utcnow
from app.reconcile.cost import ReconcileConfig
from app.reconcile.store import business_day, reconcile_day

router = APIRouter()

FIELDS = ("supplier", "carrier", "expected_count", "window_start", "window_end", "reference")


class ExpectedIn(BaseModel):
    id: uuid.UUID | None = Field(default=None, description="Client-chosen ID for safe retries")
    supplier: str = Field(min_length=1, max_length=200)
    # Same vocabulary the vision model answers in, so carrier comparison is exact. Leave it
    # out when the supplier's carrier varies; the matcher then charges the "unknown" cost.
    carrier: Carrier | None = None
    expected_count: int = Field(ge=1, le=10_000)
    window_start: AwareDatetime
    window_end: AwareDatetime
    reference: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _window_ordered(self) -> Self:
        if self.window_end < self.window_start:
            raise ValueError("window_end must not be before window_start")
        return self


def _public(row) -> dict:
    out = {k: row[k] for k in ("id", *FIELDS)}
    for k in ("window_start", "window_end"):
        out[k] = out[k].isoformat()
    out["id"] = str(out["id"])
    return out


@router.post("/v1/expected", status_code=201, dependencies=[Depends(require_admin)])
async def create_expected(request: Request, body: ExpectedIn) -> JSONResponse:
    state = request.app.state
    values = body.model_dump()
    values["id"] = body.id or uuid.uuid4()
    values["carrier"] = body.carrier.value if body.carrier else None
    t = expected_deliveries
    async with state.engine.begin() as conn:
        inserted = (
            await conn.execute(
                insert(t).values(**values).on_conflict_do_nothing().returning(t.c.id)
            )
        ).scalar_one_or_none()
        row = (await conn.execute(select(t).where(t.c.id == values["id"]))).mappings().one()
    if inserted is None and any(row[k] != values[k] for k in FIELDS):
        raise HTTPException(409, f"expected delivery {values['id']} exists with different fields")

    settings = state.settings
    tz = ZoneInfo(settings.business_timezone)
    # Reconcile now, so a delivery that already arrived is matched without waiting for the
    # timer, and the response can say where this order stands.
    await reconcile_day(
        state.engine,
        business_day(body.window_start, tz),
        tz,
        utcnow(),
        ReconcileConfig.from_settings(settings),
    )
    async with state.engine.connect() as conn:
        rec = (
            (
                await conn.execute(
                    select(reconciliations.c.status, reconciliations.c.reason).where(
                        reconciliations.c.expected_id == values["id"]
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    return JSONResponse(
        {
            "status": "created" if inserted is not None else "duplicate",
            "expected": _public(row),
            # None while the window (+ grace) is still open and nothing has matched it.
            "reconciliation": dict(rec) if rec else None,
        },
        status_code=201 if inserted is not None else 200,
    )
