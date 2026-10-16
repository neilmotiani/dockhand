"""Per-event pipeline: capture → select → extract → store delivery → reconcile.

Runs inside a queue worker (app/ingest/queue.py). Idempotency at each step:
- The delivery ID is uuid5(event ID), and `deliveries.event_id` is unique, so however often an
  event is processed (replay, retry, lease takeover) it yields at most one delivery.
- If a delivery already exists for the event (a worker crashed after storing it), the capture
  and model call are skipped and only reconciliation runs again.
- Bedrock results are cached by a hash of the frames, model and prompt version.
- Reconciliation replaces the day's rows with a deterministic set (app/reconcile/store.py).

Every stage is timed into `deliveries.latency_ms` (milliseconds) for bench/latency.py.
"""

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.capture.service import Capturer, CaptureResult, DeviceUnavailable
from app.config import Settings
from app.db.models import deliveries, extraction_cache
from app.extract.client import ExtractionOutcome, ImageFormat
from app.ingest.queue import Job, JobResult
from app.reconcile.cost import ReconcileConfig
from app.reconcile.store import business_day, reconcile_day
from app.ring.client import RingAuthError
from app.ring.tokens import TokenUnavailable

log = logging.getLogger(__name__)

# Fixed namespace: delivery ID = uuid5(namespace, ring_events.id).
DELIVERY_NAMESPACE = uuid.UUID("0b8f6a3e-5d0c-4f7e-9a7b-3c2d1e0f4a51")


class Extractor(Protocol):
    model_id: str
    prompt_version: str
    structured_output: bool

    def extract(self, images: list[tuple[bytes, ImageFormat]]) -> ExtractionOutcome: ...


def delivery_id_for(event_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(DELIVERY_NAMESPACE, str(event_id))


def cache_key(frames: list[bytes], extractor: Extractor) -> str:
    h = hashlib.sha256()
    mode = "structured" if extractor.structured_output else "prompted"
    h.update(f"{extractor.model_id}|{extractor.prompt_version}|{mode}".encode())
    for f in frames:
        h.update(hashlib.sha256(f).digest())
    return h.hexdigest()


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def _span_ms(later: datetime, earlier: datetime | None) -> float | None:
    return None if earlier is None else round((later - earlier).total_seconds() * 1000, 1)


class Pipeline:
    def __init__(
        self,
        engine: AsyncEngine,
        settings: Settings,
        capturer: Capturer,
        extractor: Extractor,
        clock: Callable[[], datetime],
    ) -> None:
        self._engine = engine
        self._s = settings
        self._capturer = capturer
        self._extractor = extractor
        self._clock = clock
        self._tz = ZoneInfo(settings.business_timezone)
        self._cfg = ReconcileConfig.from_settings(settings)

    async def __call__(self, job: Job) -> JobResult:
        delivery_id = delivery_id_for(job.id)
        existing = await self._existing_delivery(delivery_id)
        if existing is not None:
            await self._reconcile(existing)
            return JobResult("done", "delivery already stored; reconciled")

        event_time = job.occurred_at or job.received_at
        age_s = (job.claimed_at - event_time).total_seconds()
        if age_s > self._s.capture_max_event_age_s:
            # Live view now would show the scene minutes later, not the delivery.
            return JobResult("skipped", f"stale: event is {age_s:.0f} s old")

        try:
            cap = await self._capturer.capture(job.device_id, exact=job.origin != "manual")
        except DeviceUnavailable as e:
            return JobResult("skipped", f"device unavailable: {e}")
        except (TokenUnavailable, RingAuthError) as e:
            # Retrying in milliseconds cannot fix an expired Playground token, and by the time
            # a new one is pasted the event is too old to capture live.
            return JobResult("failed", f"Ring auth: {e}")
        if not cap.selection.frames:
            return JobResult("skipped", "no usable frames")

        frames = [f.jpeg for f in cap.selection.frames]
        t = time.perf_counter()
        outcome, cache_hit = await self._extract(frames)
        extract_ms = _ms(t)
        result = outcome.result
        if result.package_count == 0:
            # Someone at the door with nothing left behind: not a delivery, no ledger row.
            return JobResult("done", f"no packages ({result.carrier}): {result.reasoning[:200]}")

        t = time.perf_counter()
        paths = await asyncio.to_thread(self._save_frames, delivery_id, cap)
        save_ms = _ms(t)

        latency: dict[str, float | None] = {
            "ring_to_received": _span_ms(job.received_at, job.occurred_at),
            "queue_wait": _span_ms(job.claimed_at, job.received_at),
            **cap.latency_ms,
            "extract": extract_ms,
            "save_frames": save_ms,
        }
        t = time.perf_counter()
        ledger_at = await self._insert_delivery(
            delivery_id, job, cap, outcome, cache_hit, paths, latency
        )
        latency["ledger_insert"] = _ms(t)
        latency["event_to_ledger"] = _span_ms(ledger_at, job.occurred_at)
        latency["received_to_ledger"] = _span_ms(ledger_at, job.received_at)

        latency["reconcile"] = await self._reconcile(event_time)
        await self._update_latency(delivery_id, latency)
        return JobResult(
            "done",
            f"delivery {delivery_id}: {result.package_count} x {result.carrier}"
            f" ({cap.selection.sent}/{cap.selection.sampled} frames sent)",
        )

    async def _existing_delivery(self, delivery_id: uuid.UUID) -> datetime | None:
        async with self._engine.connect() as conn:
            stmt = select(deliveries.c.detected_at).where(deliveries.c.id == delivery_id)
            return (await conn.execute(stmt)).scalar_one_or_none()

    async def _extract(self, frames: list[bytes]) -> tuple[ExtractionOutcome, bool]:
        key = cache_key(frames, self._extractor)
        async with self._engine.connect() as conn:
            stmt = select(extraction_cache.c.outcome).where(extraction_cache.c.key == key)
            cached = (await conn.execute(stmt)).scalar_one_or_none()
        if cached is not None:
            return ExtractionOutcome.model_validate(cached), True
        images: list[tuple[bytes, ImageFormat]] = [(f, "jpeg") for f in frames]
        # boto3 is synchronous; keep it off the event loop so the webhook stays fast.
        outcome = await asyncio.to_thread(self._extractor.extract, images)
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(extraction_cache)
                .values(
                    key=key,
                    model_id=outcome.model_id,
                    prompt_version=outcome.prompt_version,
                    outcome=outcome.model_dump(mode="json"),
                )
                .on_conflict_do_nothing()
            )
        return outcome, False

    def _save_frames(self, delivery_id: uuid.UUID, cap: CaptureResult) -> list[str]:
        out = Path(self._s.media_dir) / str(delivery_id)
        out.mkdir(parents=True, exist_ok=True)
        paths = []
        for rank, f in enumerate(cap.selection.frames):
            path = out / f"frame_{rank}_i{f.index:04d}_t{f.t:05.2f}s.jpg"
            path.write_bytes(f.jpeg)
            paths.append(str(path))
        return paths

    async def _insert_delivery(
        self,
        delivery_id: uuid.UUID,
        job: Job,
        cap: CaptureResult,
        outcome: ExtractionOutcome,
        cache_hit: bool,
        paths: list[str],
        latency: dict[str, float | None],
    ) -> datetime:
        r = outcome.result
        sel = cap.selection
        # Snapshot = the best-scoring selected frame.
        best = max(range(len(sel.frames)), key=lambda i: sel.frames[i].score)
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(deliveries)
                .values(
                    id=delivery_id,
                    event_id=job.id,
                    device_id=cap.device.id,
                    # When Ring saw the event, not when we finished processing it.
                    detected_at=job.occurred_at or job.received_at,
                    carrier=r.carrier.value,
                    package_count=r.package_count,
                    label_text=r.label_text,
                    confidence=r.confidence,
                    reasoning=r.reasoning,
                    snapshot_path=paths[best],
                    clip_path=None,
                    model_id=outcome.model_id,
                    prompt_version=outcome.prompt_version,
                    frames_sampled=sel.sampled,
                    frames_sent=sel.sent,
                    usage={
                        **outcome.usage.model_dump(),
                        "attempts": outcome.attempts,
                        "structured_output": outcome.structured_output,
                        "cache_hit": cache_hit,
                        "frames_decoded": sel.decoded,
                        "capture_end": cap.clip.end_reason,
                        "stream_s": round(cap.clip.frames[-1][0], 2) if cap.clip.frames else 0,
                    },
                    latency_ms=latency,
                    source=self._s.media_source,
                )
                .on_conflict_do_nothing()
            )
        return self._clock()

    async def _update_latency(self, delivery_id: uuid.UUID, latency: dict) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(deliveries).where(deliveries.c.id == delivery_id).values(latency_ms=latency)
            )

    async def _reconcile(self, detected_at: datetime) -> float:
        run = await reconcile_day(
            self._engine, business_day(detected_at, self._tz), self._tz, self._clock(), self._cfg
        )
        return run.elapsed_ms
