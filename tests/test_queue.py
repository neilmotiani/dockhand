import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import ring_events
from app.ingest.events import manual
from app.ingest.queue import EventQueue, Job, JobResult, insert_event

T0 = datetime(2026, 10, 16, 17, 0, tzinfo=UTC)


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


async def add(engine, key: str, received_at: datetime = T0, relevant: bool = True):
    async with engine.begin() as conn:
        return await insert_event(conn, manual("cam", key, received_at), relevant, received_at)


async def row(engine, event_id):
    async with engine.connect() as conn:
        return (
            (await conn.execute(select(ring_events).where(ring_events.c.id == event_id)))
            .mappings()
            .one()
        )


async def ok(job: Job) -> JobResult:
    return JobResult("done", "stored")


async def test_insert_event_returns_none_for_duplicates(engine):
    assert await add(engine, "a") is not None
    assert await add(engine, "a") is None


async def test_claim_in_received_order_and_finish(engine):
    late = await add(engine, "late", T0 + timedelta(seconds=5))
    early = await add(engine, "early", T0)
    q = EventQueue(engine, clock=Clock(T0 + timedelta(seconds=10)))
    seen = []

    async def proc(job: Job) -> JobResult:
        seen.append(job.id)
        assert job.attempts == 1
        return JobResult("skipped", "stale")

    assert await q.run_one(proc) and await q.run_one(proc)
    assert not await q.run_one(proc)
    assert seen == [early, late]
    r = await row(engine, early)
    assert r["status"] == "skipped" and r["outcome"] == "stale" and r["finished_at"] is not None


async def test_ignored_events_are_never_claimed(engine):
    await add(engine, "x", relevant=False)
    assert await EventQueue(engine).claim() is None


async def test_failure_retries_then_fails(engine):
    eid = await add(engine, "boom")
    q = EventQueue(engine, max_attempts=2, clock=Clock(T0))

    async def boom(job: Job) -> JobResult:
        raise RuntimeError("capture failed")

    await q.run_one(boom)
    r = await row(engine, eid)
    assert r["status"] == "pending" and r["attempts"] == 1
    assert "capture failed" in r["last_error"]
    await q.run_one(boom)
    r = await row(engine, eid)
    assert r["status"] == "failed" and r["attempts"] == 2
    assert not await q.run_one(boom)


async def test_expired_lease_is_reclaimed_and_stale_result_dropped(engine):
    eid = await add(engine, "crash")
    clock = Clock(T0)
    q = EventQueue(engine, lease_s=60, clock=clock)
    first = await q.claim()
    assert first is not None
    assert await q.claim() is None  # still leased
    clock.t = T0 + timedelta(seconds=61)
    second = await q.claim()
    assert second is not None and second.id == eid and second.attempts == 2
    # The first worker finishing late must not overwrite the second worker's claim.
    await q._finish(first, status="done", outcome="late")
    assert (await row(engine, eid))["status"] == "processing"
    await q._finish(second, status="done", outcome="ok")
    assert (await row(engine, eid))["outcome"] == "ok"


async def test_lease_expiry_past_max_attempts_fails(engine):
    eid = await add(engine, "crashy")
    clock = Clock(T0)
    q = EventQueue(engine, lease_s=1, max_attempts=1, clock=clock)
    await q.claim()
    clock.t = T0 + timedelta(seconds=5)
    assert await q.run_one(ok)
    r = await row(engine, eid)
    assert r["status"] == "failed" and r["last_error"] == "lease expired"


async def test_concurrent_claims_never_share_a_row(engine):
    for i in range(20):
        await add(engine, f"e{i}", T0 + timedelta(milliseconds=i))
    q = EventQueue(engine, clock=Clock(T0 + timedelta(seconds=1)))
    jobs = await asyncio.gather(*(q.claim() for _ in range(25)))
    ids = [j.id for j in jobs if j is not None]
    assert len(ids) == 20 and len(set(ids)) == 20


async def test_worker_wakes_on_notify(engine):
    q = EventQueue(engine, idle_poll_s=30)
    done = asyncio.Event()

    async def proc(job: Job) -> JobResult:
        done.set()
        return JobResult("done", "ok")

    task = asyncio.create_task(q.worker(proc))
    try:
        await asyncio.sleep(0.05)  # worker is now idle, waiting up to 30 s
        await add(engine, "wake")
        q.notify()
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        task.cancel()
