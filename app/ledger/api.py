"""Read-only endpoints for the dashboard (and anything else that needs the ledger).

All of them require the admin token, like the write endpoints: the ledger holds a business's
supplier orders and camera stills. The dashboard calls them from its server side only, so the
token never reaches a browser; images are proxied the same way.

`date` is a business day in BUSINESS_TIMEZONE (default: today there). Responses carry the
timezone so a client can show times as the business sees them.
"""

import uuid
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.auth import require_admin
from app.ingest.queue import utcnow
from app.ledger import metrics, queries, reset, suppliers
from app.reconcile.cost import ReconcileConfig
from app.reconcile.store import business_day, day_bounds

router = APIRouter(dependencies=[Depends(require_admin)])

# Evidence files never change once written (one directory per delivery ID).
MEDIA_CACHE = "private, max-age=86400, immutable"


def _tz(request: Request) -> ZoneInfo:
    return ZoneInfo(request.app.state.settings.business_timezone)


def _day(request: Request, day: date | None) -> tuple[date, ZoneInfo]:
    tz = _tz(request)
    return day or business_day(utcnow(), tz), tz


def _envelope(day: date, tz: ZoneInfo, **body) -> dict:
    now = utcnow()
    return {
        "date": day.isoformat(),
        "today": business_day(now, tz).isoformat(),
        "timezone": tz.key,
        "generated_at": now,
        **body,
    }


DateQuery = Query(default=None, alias="date", description="YYYY-MM-DD; default today")
SimQuery = Query(default=True, description="Include simulated demo history")


@router.get("/v1/deliveries")
async def list_deliveries(
    request: Request, day: date | None = DateQuery, include_simulated: bool = SimQuery
) -> dict:
    day, tz = _day(request, day)
    async with request.app.state.engine.connect() as conn:
        rows = await queries.day_deliveries(conn, day, tz, include_simulated)
    return _envelope(day, tz, deliveries=rows)


@router.get("/v1/expected")
async def list_expected(
    request: Request, day: date | None = DateQuery, include_simulated: bool = SimQuery
) -> dict:
    day, tz = _day(request, day)
    async with request.app.state.engine.connect() as conn:
        rows = await queries.day_expected(conn, day, tz, include_simulated)
    return _envelope(day, tz, expected=rows)


@router.get("/v1/discrepancies")
async def list_discrepancies(
    request: Request, day: date | None = DateQuery, include_simulated: bool = SimQuery
) -> dict:
    day, tz = _day(request, day)
    async with request.app.state.engine.connect() as conn:
        rows = await queries.day_discrepancies(conn, day, tz, include_simulated)
    return _envelope(day, tz, discrepancies=rows)


@router.get("/v1/activity")
async def list_activity(
    request: Request,
    day: date | None = DateQuery,
    limit: int = Query(default=10, ge=1, le=100),
) -> dict:
    """Recent pipeline events of the day (queued, processing, done, skipped, failed)."""
    day, tz = _day(request, day)
    start, end = day_bounds(day, tz)
    async with request.app.state.engine.connect() as conn:
        rows = await queries.recent_activity(conn, start, end, limit)
    return _envelope(day, tz, events=rows)


@router.get("/v1/suppliers")
async def list_suppliers(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    include_simulated: bool = SimQuery,
) -> dict:
    """Per-supplier on-time %, short-delivery rate, missed deliveries and boxes short over the
    last `days` business days, today included (app/ledger/suppliers.py)."""
    today, tz = _day(request, None)
    async with request.app.state.engine.connect() as conn:
        body = await suppliers.supplier_stats(conn, today, tz, days, include_simulated)
    return _envelope(today, tz, **body)


class ResetRequest(BaseModel):
    day: date | None = Field(default=None, alias="date", description="Business day; default today")
    dry_run: bool = Field(default=True, description="Count what would be deleted, delete nothing")


@router.post("/v1/admin/reset-day")
async def reset_demo_day(request: Request, body: ResetRequest) -> dict:
    """Clear a day's Playground deliveries, orders and events so a demo can be re-recorded.
    Needs the admin token and ALLOW_DEMO_RESET=true (404 otherwise). Dry run by default."""
    settings = request.app.state.settings
    if not settings.allow_demo_reset:
        raise HTTPException(404, "demo reset is disabled (set ALLOW_DEMO_RESET=true)")
    day, tz = _day(request, body.day)
    try:
        result = await reset.reset_day(
            request.app.state.engine,
            day,
            tz,
            Path(settings.media_dir),
            utcnow(),
            ReconcileConfig.from_settings(settings),
            dry_run=body.dry_run,
        )
    except reset.ResetBlocked as e:
        raise HTTPException(409, str(e)) from None
    return result.as_dict()


@router.get("/v1/metrics")
async def get_metrics(request: Request) -> dict:
    return metrics.load(Path(request.app.state.settings.results_root))


async def _media(request: Request, delivery_id: uuid.UUID) -> dict:
    async with request.app.state.engine.connect() as conn:
        row = await queries.delivery_media_paths(conn, delivery_id)
    if row is None:
        raise HTTPException(404, "no such delivery")
    return row


def _serve(request: Request, path: str | Path | None, media_type: str) -> FileResponse:
    if not path:
        raise HTTPException(404, "not available for this delivery")
    p = Path(path).resolve()
    # Paths come from our own database, but only ever serve files under MEDIA_DIR.
    root = Path(request.app.state.settings.media_dir).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        raise HTTPException(404, "media file not found")
    return FileResponse(p, media_type=media_type, headers={"Cache-Control": MEDIA_CACHE})


@router.get("/v1/deliveries/{delivery_id}/snapshot")
async def get_snapshot(request: Request, delivery_id: uuid.UUID) -> FileResponse:
    row = await _media(request, delivery_id)
    return _serve(request, row["snapshot_path"], "image/jpeg")


@router.get("/v1/deliveries/{delivery_id}/frames/{rank}")
async def get_frame(request: Request, delivery_id: uuid.UUID, rank: int) -> FileResponse:
    """The rank-th frame sent to the model (file names: app/pipeline.py `_save_frames`)."""
    row = await _media(request, delivery_id)
    if not row["snapshot_path"] or not 0 <= rank < (row["frames_sent"] or 0):
        raise HTTPException(404, "no such frame")
    matches = sorted(Path(row["snapshot_path"]).parent.glob(f"frame_{rank}_*.jpg"))
    return _serve(request, matches[0] if matches else None, "image/jpeg")


@router.get("/v1/deliveries/{delivery_id}/clip")
async def get_clip(request: Request, delivery_id: uuid.UUID) -> FileResponse:
    row = await _media(request, delivery_id)
    return _serve(request, row["clip_path"], "video/mp4")
