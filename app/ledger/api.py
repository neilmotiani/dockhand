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

from app.auth import require_admin
from app.ingest.queue import utcnow
from app.ledger import metrics, queries
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


@router.get("/v1/deliveries")
async def list_deliveries(request: Request, day: date | None = DateQuery) -> dict:
    day, tz = _day(request, day)
    async with request.app.state.engine.connect() as conn:
        rows = await queries.day_deliveries(conn, day, tz)
    return _envelope(day, tz, deliveries=rows)


@router.get("/v1/expected")
async def list_expected(request: Request, day: date | None = DateQuery) -> dict:
    day, tz = _day(request, day)
    async with request.app.state.engine.connect() as conn:
        rows = await queries.day_expected(conn, day, tz)
    return _envelope(day, tz, expected=rows)


@router.get("/v1/discrepancies")
async def list_discrepancies(request: Request, day: date | None = DateQuery) -> dict:
    day, tz = _day(request, day)
    async with request.app.state.engine.connect() as conn:
        rows = await queries.day_discrepancies(conn, day, tz)
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
