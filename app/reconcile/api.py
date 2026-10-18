"""POST /v1/expected: record a delivery the business expects, then reconcile its day.

Operator endpoint (admin token), used by the dashboard's expected-list editor and the seed
script. Idempotent: the client may send its own `id` (rules in app/reconcile/expected.py).
"""

import uuid
from typing import Self
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from app.auth import require_admin
from app.extract.schemas import Carrier
from app.ingest.queue import utcnow
from app.reconcile.cost import ReconcileConfig
from app.reconcile.expected import ExpectedConflict, add_expected

router = APIRouter()


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


def _public(row: dict) -> dict:
    out = dict(row)
    for k in ("window_start", "window_end"):
        out[k] = out[k].isoformat()
    out["id"] = str(out["id"])
    return out


@router.post("/v1/expected", status_code=201, dependencies=[Depends(require_admin)])
async def create_expected(request: Request, body: ExpectedIn) -> JSONResponse:
    state = request.app.state
    settings = state.settings
    values = body.model_dump()
    values["id"] = body.id or uuid.uuid4()
    values["carrier"] = body.carrier.value if body.carrier else None
    try:
        res = await add_expected(
            state.engine,
            values,
            ZoneInfo(settings.business_timezone),
            utcnow(),
            ReconcileConfig.from_settings(settings),
        )
    except ExpectedConflict as e:
        raise HTTPException(409, str(e)) from None
    return JSONResponse(
        {
            "status": "created" if res.created else "duplicate",
            "expected": _public(res.expected),
            "reconciliation": res.reconciliation,
        },
        status_code=201 if res.created else 200,
    )
