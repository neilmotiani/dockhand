"""Durable event queue on top of the `ring_events` table.

The webhook handler acks only after the event row is committed, so an event Ring has seen a
200 for is never lost to a crash. Dedupe is the table's unique constraints (`request_id`,
`ring_event_id`) plus `INSERT ... ON CONFLICT DO NOTHING`: the database decides, so two
concurrent deliveries of the same webhook cannot both get in.

Workers claim one pending row at a time with `FOR UPDATE SKIP LOCKED`, which lets several
workers (or processes) share the table without claiming the same row. A claim is a lease: a
row stuck in `processing` for longer than `queue_lease_s` (its worker died) is claimed again.
An in-process asyncio.Event wakes idle workers as soon as a row is inserted; the idle poll is
only the fallback for rows inserted by another process.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.models import ring_events
from app.ingest.events import NormalizedEvent

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


async def insert_event(
    conn: AsyncConnection, event: NormalizedEvent, relevant: bool, received_at: datetime
) -> uuid.UUID | None:
    """Insert the event; return its new ID, or None if it was already stored (a duplicate)."""
    stmt = (
        insert(ring_events)
        .values(
            id=uuid.uuid4(),
            request_id=event.request_id,
            ring_event_id=event.ring_event_id,
            origin=event.origin,
            device_id=event.device_id,
            event_type=event.event_type,
            sub_type=event.sub_type,
            occurred_at=event.occurred_at,
            received_at=received_at,
            raw=event.raw,
            relevant=relevant,
            status="pending" if relevant else "ignored",
        )
        # No conflict target: a clash on either unique column means we have seen this event.
        .on_conflict_do_nothing()
        .returning(ring_events.c.id)
    )
    return (await conn.execute(stmt)).scalar_one_or_none()


@dataclass(frozen=True)
class Job:
    id: uuid.UUID
    request_id: str
    origin: str
    device_id: str | None
    event_type: str
    sub_type: str | None
    occurred_at: datetime | None
    received_at: datetime
    claimed_at: datetime
    attempts: int


@dataclass(frozen=True)
class JobResult:
    # "failed" from a processor is final (no retry), for errors a retry cannot fix.
    status: Literal["done", "skipped", "failed"]
    outcome: str


Processor = Callable[[Job], Awaitable[JobResult]]


class EventQueue:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        max_attempts: int = 3,
        lease_s: float = 300.0,
        idle_poll_s: float = 1.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._engine = engine
        self._max_attempts = max_attempts
        self._lease = timedelta(seconds=lease_s)
        self._idle_poll_s = idle_poll_s
        self._clock = clock
        self._wake = asyncio.Event()

    def notify(self) -> None:
        self._wake.set()

    async def claim(self) -> Job | None:
        now = self._clock()
        t = ring_events
        claimable = (
            select(t.c.id)
            .where(
                or_(
                    t.c.status == "pending",
                    and_(t.c.status == "processing", t.c.claimed_at < now - self._lease),
                )
            )
            .order_by(t.c.received_at)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        stmt = (
            update(t)
            .where(t.c.id == claimable)
            .values(status="processing", claimed_at=now, attempts=t.c.attempts + 1)
            .returning(
                t.c.id,
                t.c.request_id,
                t.c.origin,
                t.c.device_id,
                t.c.event_type,
                t.c.sub_type,
                t.c.occurred_at,
                t.c.received_at,
                t.c.claimed_at,
                t.c.attempts,
            )
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(stmt)).mappings().one_or_none()
        return Job(**row) if row else None

    async def _finish(self, job: Job, **values) -> None:
        t = ring_events
        async with self._engine.begin() as conn:
            # Guard on claimed_at: if our lease expired and another worker re-claimed the row,
            # its result wins and ours is dropped.
            await conn.execute(
                update(t)
                .where(t.c.id == job.id, t.c.status == "processing")
                .where(t.c.claimed_at == job.claimed_at)
                .values(**values)
            )

    async def run_one(self, processor: Processor) -> bool:
        """Claim and process one job. Returns False if the queue was empty."""
        job = await self.claim()
        if job is None:
            return False
        if job.attempts > self._max_attempts:
            # Only reachable through lease expiry: the worker died on every attempt.
            await self._finish(
                job, status="failed", finished_at=self._clock(), last_error="lease expired"
            )
            return True
        try:
            result = await processor(job)
        except Exception as e:  # one bad event must not stop the worker
            log.exception("event %s failed (attempt %d)", job.request_id, job.attempts)
            final = job.attempts >= self._max_attempts
            await self._finish(
                job,
                status="failed" if final else "pending",
                finished_at=self._clock() if final else None,
                last_error=f"{type(e).__name__}: {e}"[:2000],
            )
            return True
        await self._finish(
            job, status=result.status, outcome=result.outcome, finished_at=self._clock()
        )
        return True

    async def worker(self, processor: Processor) -> None:
        while True:
            # Clear before claiming, so a notify() that lands while we find the queue empty
            # still wakes us instead of waiting out the idle poll.
            self._wake.clear()
            try:
                if await self.run_one(processor):
                    continue
            except Exception:
                # Database unavailable, etc. Back off and retry rather than dying.
                log.exception("queue worker error")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._idle_poll_s)
            except TimeoutError:
                pass
