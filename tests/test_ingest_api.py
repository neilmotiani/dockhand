import asyncio
import contextlib
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import ring_events
from app.ingest.signature import sign
from app.main import create_app

DOCS = Path(__file__).parent / "fixtures" / "ring" / "docs"
SECRET = "test-signing-key"
ADMIN = "test-admin-token"


def payload(request_id: str = "req-0001", event_id: str = "evt-0001", **data) -> dict:
    p = json.loads((DOCS / "webhook_motion_human.json").read_text())["payload"]
    p["meta"]["request_id"] = request_id
    p["data"]["id"] = event_id
    p["data"].update(data)
    return p


def signed(p: dict) -> tuple[bytes, dict]:
    body = json.dumps(p).encode()
    return body, {"X-Signature": sign(SECRET, body), "Content-Type": "application/json"}


@contextlib.asynccontextmanager
async def client_for(engine, **overrides):
    settings = Settings(_env_file=None, ring_webhook_secret=SECRET, admin_token=ADMIN, **overrides)
    app = create_app(settings, engine=engine, start_background=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.app = app
            yield c


async def count(engine, *where) -> int:
    async with engine.connect() as conn:
        stmt = select(func.count()).select_from(ring_events).where(*where)
        return (await conn.execute(stmt)).scalar_one()


async def test_webhook_queues_relevant_event(engine):
    async with client_for(engine) as c:
        body, headers = signed(payload())
        r = await c.post("/webhooks/ring", content=body, headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    async with engine.connect() as conn:
        row = (await conn.execute(select(ring_events))).mappings().one()
    assert row["status"] == "pending" and row["relevant"] is True
    assert row["request_id"] == "req-0001" and row["origin"] == "webhook"
    assert row["raw"]["data"]["subType"] == "human"


async def test_webhook_replay_is_deduped(engine):
    async with client_for(engine) as c:
        body, headers = signed(payload())
        statuses = [
            (await c.post("/webhooks/ring", content=body, headers=headers)).json()["status"]
            for _ in range(3)
        ]
    assert statuses == ["queued", "duplicate", "duplicate"]
    assert await count(engine) == 1


async def test_concurrent_duplicate_deliveries_insert_once(engine):
    async with client_for(engine) as c:
        body, headers = signed(payload())
        rs = await asyncio.gather(
            *(c.post("/webhooks/ring", content=body, headers=headers) for _ in range(10))
        )
    assert all(r.status_code == 200 for r in rs)
    assert sorted(r.json()["status"] for r in rs).count("queued") == 1
    assert await count(engine) == 1


async def test_same_ring_event_under_new_request_id_is_deduped(engine):
    async with client_for(engine) as c:
        for rid in ("req-a", "req-b"):
            body, headers = signed(payload(request_id=rid, event_id="evt-same"))
            await c.post("/webhooks/ring", content=body, headers=headers)
    assert await count(engine) == 1


async def test_webhook_rejects_bad_signature(engine):
    async with client_for(engine) as c:
        body, headers = signed(payload())
        headers["X-Signature"] = sign("wrong-key", body)
        r = await c.post("/webhooks/ring", content=body, headers=headers)
        r2 = await c.post("/webhooks/ring", content=body)
    assert r.status_code == 401 and r2.status_code == 401
    assert await count(engine) == 0


async def test_webhook_refuses_when_secret_unset(engine):
    settings = Settings(_env_file=None)
    assert settings.ring_webhook_secret is None
    app = create_app(settings, engine=engine, start_background=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.post("/webhooks/ring", content=b"{}")
    assert r.status_code == 503


async def test_webhook_rejects_signed_garbage(engine):
    async with client_for(engine) as c:
        body = b"not json"
        headers = {"X-Signature": sign(SECRET, body)}
        r = await c.post("/webhooks/ring", content=body, headers=headers)
        p = payload()
        del p["meta"]["request_id"]
        body2, headers2 = signed(p)
        r2 = await c.post("/webhooks/ring", content=body2, headers=headers2)
    assert r.status_code == 400 and r2.status_code == 400


async def test_irrelevant_event_is_stored_but_not_queued(engine):
    async with client_for(engine) as c:
        body, headers = signed(payload(type="device_online"))
        r = await c.post("/webhooks/ring", content=body, headers=headers)
        assert r.json()["status"] == "ignored"
    assert await count(engine, ring_events.c.status == "ignored") == 1


async def test_relevance_is_configurable(engine):
    async with client_for(engine, ingest_relevant_events=["button_press"]) as c:
        body, headers = signed(payload())
        assert (await c.post("/webhooks/ring", content=body, headers=headers)).json()[
            "status"
        ] == "ignored"


async def test_queue_is_notified_on_insert(engine):
    async with client_for(engine) as c:
        queue = c.app.state.queue
        assert not queue._wake.is_set()
        body, headers = signed(payload())
        await c.post("/webhooks/ring", content=body, headers=headers)
        assert queue._wake.is_set()


# --- manual trigger -----------------------------------------------------------------------


async def test_trigger_requires_admin_token(engine):
    async with client_for(engine) as c:
        r = await c.post("/v1/trigger", json={})
        r2 = await c.post("/v1/trigger", json={}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r2.status_code == 401
    assert await count(engine) == 0


async def test_trigger_disabled_without_admin_token(engine):
    app = create_app(
        Settings(_env_file=None, ring_webhook_secret=SECRET), engine=engine, start_background=False
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.post("/v1/trigger", json={}, headers={"Authorization": "Bearer x"})
    assert r.status_code == 404


@pytest.mark.parametrize("key", [None, "demo-1"])
async def test_trigger_enqueues_and_respects_idempotency_key(engine, key):
    auth = {"Authorization": f"Bearer {ADMIN}"}
    async with client_for(engine) as c:
        req = {"device": "Back", "idempotency_key": key}
        r1 = await c.post("/v1/trigger", json=req, headers=auth)
        r2 = await c.post("/v1/trigger", json=req, headers=auth)
    assert r1.status_code == 202 and r1.json()["status"] == "queued"
    if key is None:
        assert r2.json()["status"] == "queued"
        assert await count(engine) == 2
    else:
        assert r2.json()["status"] == "duplicate"
        assert await count(engine) == 1
    async with engine.connect() as conn:
        row = (await conn.execute(select(ring_events).limit(1))).mappings().one()
    assert row["origin"] == "manual" and row["device_id"] == "Back"
