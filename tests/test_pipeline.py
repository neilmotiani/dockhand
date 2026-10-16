from datetime import timedelta

from sqlalchemy import func, select

from app.db.models import deliveries, extraction_cache, reconciliations, ring_events
from app.ingest.events import from_webhook, manual
from app.ingest.queue import EventQueue, insert_event
from app.pipeline import cache_key, delivery_id_for
from tests.pipeline_fakes import T0, Clock, FakeCapturer, FakeExtractor, add_expected, make_pipeline


def webhook(request_id="req-1", event_id="evt-1", at=T0, device="ava1.ring.device.TEST"):
    return from_webhook(
        {
            "meta": {"version": "1.1", "request_id": request_id, "account_id": "a"},
            "data": {
                "id": event_id,
                "type": "motion_detected",
                "subType": "human",
                "attributes": {"source": device, "timestamp": int(at.timestamp() * 1000)},
            },
        }
    )


async def enqueue(engine, event, received_at):
    async with engine.begin() as conn:
        return await insert_event(conn, event, True, received_at)


async def rows(engine, table):
    async with engine.connect() as conn:
        return (await conn.execute(select(table))).mappings().all()


async def event_row(engine, event_id):
    async with engine.connect() as conn:
        stmt = select(ring_events).where(ring_events.c.id == event_id)
        return (await conn.execute(stmt)).mappings().one()


async def test_event_becomes_delivery_and_reconciles(engine, tmp_path):
    clock = Clock(T0 + timedelta(seconds=9))
    pipe, capturer, extractor = make_pipeline(engine, tmp_path, clock)
    expected_id = await add_expected(engine, count=2)
    ev_id = await enqueue(engine, webhook(), T0 + timedelta(seconds=1))
    q = EventQueue(engine, clock=clock)

    assert await q.run_one(pipe)

    ev = await event_row(engine, ev_id)
    assert ev["status"] == "done", ev["last_error"]
    assert "2 x UPS" in ev["outcome"] and "3/10 frames" in ev["outcome"]
    assert capturer.calls == [("ava1.ring.device.TEST", True)]  # exact device match

    (d,) = await rows(engine, deliveries)
    assert d["id"] == delivery_id_for(ev_id)
    assert d["carrier"] == "UPS" and d["package_count"] == 2
    assert d["label_text"] == ["1Z999"] and d["detected_at"] == T0
    assert d["frames_sampled"] == 10 and d["frames_sent"] == 3
    assert d["source"] == "ring_playground" and d["model_id"] == "fake-model"
    assert d["usage"]["cache_hit"] is False and d["usage"]["input_tokens"] == 1000
    # Snapshot is the best-scoring selected frame, saved under the delivery's media dir.
    assert d["snapshot_path"].endswith(".jpg") and str(d["id"]) in d["snapshot_path"]
    assert len(list((tmp_path / "media" / str(d["id"])).glob("*.jpg"))) == 3

    lat = d["latency_ms"]
    assert lat["ring_to_received"] == 1000.0
    assert lat["queue_wait"] == 8000.0
    assert lat["event_to_ledger"] == 9000.0 and lat["received_to_ledger"] == 8000.0
    for stage in (
        "device_resolve",
        "whep_setup",
        "capture",
        "select",
        "extract",
        "save_frames",
        "ledger_insert",
        "reconcile",
    ):
        assert isinstance(lat[stage], float), stage

    (r,) = await rows(engine, reconciliations)
    assert r["status"] == "matched" and r["expected_id"] == expected_id
    assert r["delivery_id"] == d["id"]


async def test_short_delivery_is_flagged(engine, tmp_path):
    clock = Clock(T0 + timedelta(seconds=5))
    pipe, _, _ = make_pipeline(engine, tmp_path, clock, extractor=FakeExtractor(count=1))
    await add_expected(engine, count=3)
    await enqueue(engine, webhook(), T0)
    await EventQueue(engine, clock=clock).run_one(pipe)
    (r,) = await rows(engine, reconciliations)
    assert r["status"] == "short" and "Short by 2" in r["reason"]


async def test_stale_event_is_skipped_without_capture(engine, tmp_path):
    clock = Clock(T0 + timedelta(minutes=10))
    pipe, capturer, extractor = make_pipeline(engine, tmp_path, clock)
    ev_id = await enqueue(engine, webhook(), T0 + timedelta(minutes=9))
    await EventQueue(engine, clock=clock).run_one(pipe)
    ev = await event_row(engine, ev_id)
    assert ev["status"] == "skipped" and ev["outcome"].startswith("stale")
    assert capturer.calls == [] and extractor.calls == 0


async def test_unavailable_device_is_skipped_not_retried(engine, tmp_path):
    clock = Clock(T0 + timedelta(seconds=3))
    capturer = FakeCapturer(unavailable={"ava1.ring.device.TEST"})
    pipe, _, _ = make_pipeline(engine, tmp_path, clock, capturer=capturer)
    ev_id = await enqueue(engine, webhook(), T0)
    await EventQueue(engine, clock=clock).run_one(pipe)
    ev = await event_row(engine, ev_id)
    assert ev["status"] == "skipped" and "offline" in ev["outcome"] and ev["attempts"] == 1


async def test_zero_packages_stores_no_delivery(engine, tmp_path):
    clock = Clock(T0 + timedelta(seconds=3))
    pipe, _, _ = make_pipeline(engine, tmp_path, clock, extractor=FakeExtractor(count=0))
    ev_id = await enqueue(engine, webhook(), T0)
    await EventQueue(engine, clock=clock).run_one(pipe)
    assert (await event_row(engine, ev_id))["outcome"].startswith("no packages")
    assert await rows(engine, deliveries) == []


async def test_manual_trigger_uses_name_lookup(engine, tmp_path):
    clock = Clock(T0 + timedelta(seconds=1))
    pipe, capturer, _ = make_pipeline(engine, tmp_path, clock)
    await enqueue(engine, manual("Back Door", "k", T0), T0)
    await EventQueue(engine, clock=clock).run_one(pipe)
    assert capturer.calls == [("Back Door", False)]
    (d,) = await rows(engine, deliveries)
    assert d["latency_ms"]["ring_to_received"] == 0.0


async def test_reprocessing_after_crash_reuses_stored_delivery(engine, tmp_path):
    """A worker that dies after storing the delivery but before marking the event done: the
    retry must not capture or call the model again, and must not add a second delivery."""
    clock = Clock(T0 + timedelta(seconds=5))
    pipe, capturer, extractor = make_pipeline(engine, tmp_path, clock)
    ev_id = await enqueue(engine, webhook(), T0)
    q = EventQueue(engine, lease_s=30, clock=clock)
    job = await q.claim()
    await pipe(job)  # stored, but the event is never marked done
    clock.t += timedelta(minutes=5)  # lease expired, and the event is now stale too
    assert await q.run_one(pipe)
    ev = await event_row(engine, ev_id)
    assert ev["status"] == "done" and "already stored" in ev["outcome"]
    assert capturer.calls == [("ava1.ring.device.TEST", True)] and extractor.calls == 1
    async with engine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(deliveries))).scalar_one() == 1


async def test_identical_frames_hit_the_extraction_cache(engine, tmp_path):
    clock = Clock(T0 + timedelta(seconds=5))
    capturer = FakeCapturer(same_frames=True)
    pipe, _, extractor = make_pipeline(engine, tmp_path, clock, capturer=capturer)
    await enqueue(engine, webhook("r1", "e1"), T0)
    await enqueue(engine, webhook("r2", "e2"), T0)
    q = EventQueue(engine, clock=clock)
    await q.run_one(pipe)
    await q.run_one(pipe)
    assert extractor.calls == 1
    ds = await rows(engine, deliveries)
    assert sorted(d["usage"]["cache_hit"] for d in ds) == [False, True]
    assert len(await rows(engine, extraction_cache)) == 1


def test_cache_key_depends_on_model_prompt_mode_and_frames():
    a, b = FakeExtractor(), FakeExtractor()
    base = cache_key([b"x", b"y"], a)
    assert cache_key([b"x", b"y"], b) == base
    assert cache_key([b"y", b"x"], a) != base
    b.prompt_version = "v2"
    assert cache_key([b"x", b"y"], b) != base
    b.prompt_version, b.structured_output = "v1", True
    assert cache_key([b"x", b"y"], b) != base


async def test_expired_token_fails_once_without_retry(engine, tmp_path):
    from app.ring.tokens import TokenUnavailable

    class NoToken(FakeCapturer):
        async def capture(self, query, *, exact):
            self.calls.append((query, exact))
            raise TokenUnavailable("RING_ACCESS_TOKEN is unset or expired")

    clock = Clock(T0 + timedelta(seconds=2))
    pipe, capturer, _ = make_pipeline(engine, tmp_path, clock, capturer=NoToken())
    ev_id = await enqueue(engine, webhook(), T0)
    q = EventQueue(engine, clock=clock)
    assert await q.run_one(pipe)
    assert not await q.run_one(pipe)
    ev = await event_row(engine, ev_id)
    assert ev["status"] == "failed" and ev["attempts"] == 1
    assert ev["outcome"].startswith("Ring auth") and len(capturer.calls) == 1
