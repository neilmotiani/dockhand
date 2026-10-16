"""Chaos test (SPEC.md section 6): replay every webhook 3x in random order, interleaved with
Event History polls of the same events, and require zero duplicate ledger entries.

Runs the real HTTP endpoint, queue, pipeline and reconciliation against Postgres; only WHEP
capture and Bedrock are faked.
"""

import asyncio
import json
import random
from collections import Counter
from datetime import timedelta

import httpx
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import deliveries, reconciliations, ring_events
from app.ingest.poller import HistoryPoller
from app.ingest.queue import EventQueue
from app.ingest.signature import sign
from app.main import create_app
from app.reconcile.cost import ReconcileConfig
from app.reconcile.store import business_day, reconcile_day
from tests.pipeline_fakes import T0, Clock, add_expected, make_pipeline
from tests.test_reconcile_store import LA

SECRET = "chaos-key"
N_EVENTS = 12
REPLAYS = 3


def webhook_body(i: int) -> bytes:
    at = T0 + timedelta(minutes=7 * i)
    return json.dumps(
        {
            "meta": {"version": "1.1", "request_id": f"req-{i}", "account_id": "acct"},
            "data": {
                "id": f"evt-{i}",
                "type": "motion_detected",
                "subType": "human",
                "attributes": {
                    "source": "ava1.ring.device.TEST",
                    "source_type": "devices",
                    "timestamp": int(at.timestamp() * 1000),
                },
            },
        }
    ).encode()


def history_page(ids: list[int]) -> dict:
    return {
        "data": [
            {
                "type": "history-events",
                "id": f"evt-{i}",  # same event IDs as the webhooks
                "attributes": {
                    "event_type": "motion",
                    "start": int((T0 + timedelta(minutes=7 * i)).timestamp() * 1000),
                    "end": int((T0 + timedelta(minutes=7 * i, seconds=20)).timestamp() * 1000),
                },
                "relationships": {
                    "source": {"data": {"type": "devices", "id": "ava1.ring.device.TEST"}}
                },
            }
            for i in ids
        ]
    }


async def counts(engine) -> dict[str, int]:
    async with engine.connect() as conn:
        out = {}
        for t in (ring_events, deliveries, reconciliations):
            out[t.name] = (await conn.execute(select(func.count()).select_from(t))).scalar_one()
        return out


async def test_replayed_webhooks_create_no_duplicates(engine, tmp_path):
    rng = random.Random(20261016)
    for k in range(4):
        await add_expected(
            engine,
            supplier=f"Supplier {k}",
            count=2,
            start=T0 + timedelta(minutes=20 * k),
            end=T0 + timedelta(minutes=20 * k + 30),
        )

    # Every webhook three times, shuffled, sent in concurrent bursts.
    sends = [i for i in range(N_EVENTS) for _ in range(REPLAYS)]
    rng.shuffle(sends)

    settings = Settings(_env_file=None, ring_webhook_secret=SECRET)
    app = create_app(settings, engine=engine, start_background=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            statuses: Counter[str] = Counter()
            for burst in range(0, len(sends), 6):
                bodies = [webhook_body(i) for i in sends[burst : burst + 6]]
                rs = await asyncio.gather(
                    *(
                        client.post(
                            "/webhooks/ring",
                            content=b,
                            headers={"X-Signature": sign(SECRET, b)},
                        )
                        for b in bodies
                    )
                )
                assert all(r.status_code == 200 for r in rs)
                statuses.update(r.json()["status"] for r in rs)
    assert statuses == {"queued": N_EVENTS, "duplicate": N_EVENTS * (REPLAYS - 1)}

    # The poller then reports the same events (overlapping pages, twice).
    class HistoryRing:
        async def list_devices(self):
            from tests.pipeline_fakes import CAM

            return [CAM]

        async def history_events(self, device_id, event_types):
            ids = rng.sample(range(N_EVENTS), N_EVENTS // 2)
            return history_page(ids)["data"]

    clock = Clock(T0 + timedelta(seconds=1))
    poller = HistoryPoller(
        HistoryRing(),
        engine,
        event_types="motion.human,ding",
        relevant_rules=settings.ingest_relevant_events,
        lookback=timedelta(days=1),
        clock=clock,
    )
    assert await poller.poll_once() == 0
    assert await poller.poll_once() == 0

    # Drain the queue with two concurrent workers. The fake clock places each claim just
    # after its event, so none is stale.
    pipe, capturer, extractor = make_pipeline(
        engine, tmp_path, clock, capture_max_event_age_s=10**6
    )
    queue = EventQueue(engine, clock=clock)

    async def drain():
        while await queue.run_one(pipe):
            pass

    await asyncio.gather(drain(), drain())

    c = await counts(engine)
    assert c["ring_events"] == N_EVENTS
    assert c["deliveries"] == N_EVENTS
    assert extractor.calls == N_EVENTS and len(capturer.calls) == N_EVENTS

    # Replay everything again after processing: still nothing new.
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for i in range(N_EVENTS):
                b = webhook_body(i)
                r = await client.post(
                    "/webhooks/ring", content=b, headers={"X-Signature": sign(SECRET, b)}
                )
                assert r.json()["status"] == "duplicate"
    assert not await queue.run_one(pipe)

    # Reconciliation: each delivery and each expected delivery appears in exactly one row,
    # and re-running produces the identical set.
    async with engine.connect() as conn:
        rows = (await conn.execute(select(reconciliations))).mappings().all()
    per_delivery = Counter(r["delivery_id"] for r in rows if r["delivery_id"])
    per_expected = Counter(r["expected_id"] for r in rows if r["expected_id"])
    assert len(per_delivery) == N_EVENTS and set(per_delivery.values()) == {1}
    assert set(per_expected.values()) <= {1}
    before = sorted((r["id"], r["status"]) for r in rows)
    await reconcile_day(engine, business_day(T0, LA), LA, clock(), ReconcileConfig())
    async with engine.connect() as conn:
        after = sorted(
            (r["id"], r["status"]) for r in (await conn.execute(select(reconciliations))).mappings()
        )
    assert after == before
