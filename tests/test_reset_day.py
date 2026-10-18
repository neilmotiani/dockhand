import contextlib
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select, update

from app.config import Settings
from app.db.models import deliveries, expected_deliveries, reconciliations, ring_events
from app.main import create_app
from scripts.reset_demo_day import describe, reset
from tests.pipeline_fakes import T0, add_expected
from tests.test_ledger_api import ADMIN, AUTH, DAY, add_delivery, add_event, reconcile


@contextlib.asynccontextmanager
async def client_for(engine, media_dir: Path, allow: bool = True):
    settings = Settings(
        _env_file=None, admin_token=ADMIN, media_dir=media_dir, allow_demo_reset=allow
    )
    app = create_app(settings, engine=engine, start_background=False, token_env_file=None)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH
        ) as c:
            yield c


async def count(engine, table) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(select(func.count()).select_from(table))).scalar_one()


@pytest.fixture
def media(tmp_path) -> Path:
    p = tmp_path / "media"
    p.mkdir()
    return p


async def demo_day(engine, media):
    """Two Playground deliveries and two orders on DAY, one delivery and order the next day,
    a failed event on DAY, all reconciled."""
    await add_expected(engine, supplier="Sysco", carrier=None, count=1)
    await add_expected(engine, supplier="US Foods", carrier=None, count=3)
    a = await add_delivery(engine, media, T0, carrier="Unknown")
    b = await add_delivery(engine, media, T0 + timedelta(minutes=5), carrier="Unknown")
    await add_event(engine, T0 + timedelta(minutes=9), status="failed", outcome="Ring auth")
    nxt = T0 + timedelta(days=1)
    await add_expected(engine, supplier="Next", count=1, start=nxt, end=nxt + timedelta(hours=1))
    other = await add_delivery(engine, media, nxt)
    await reconcile(engine, T0 + timedelta(hours=1))
    return a, b, other


async def test_dry_run_counts_and_deletes_nothing(engine, media):
    await demo_day(engine, media)
    before = [await count(engine, t) for t in (deliveries, expected_deliveries, ring_events)]
    async with client_for(engine, media) as c:
        r = await c.post("/v1/admin/reset-day", json={"date": DAY})
    assert r.status_code == 200
    assert r.json() == {
        "date": DAY,
        "dry_run": True,
        "deliveries": 2,
        "expected": 2,
        "reconciliations": 2,
        "events": 3,
        "media_dirs": 2,
        "kept_device_deliveries": 0,
        "kept_simulated": 0,
    }
    after = [await count(engine, t) for t in (deliveries, expected_deliveries, ring_events)]
    assert after == before
    assert len(list(media.iterdir())) == 3


async def test_reset_clears_only_that_day_and_is_repeatable(engine, media):
    a, b, other = await demo_day(engine, media)
    async with client_for(engine, media) as c:
        r1 = await c.post("/v1/admin/reset-day", json={"date": DAY, "dry_run": False})
        r2 = await c.post("/v1/admin/reset-day", json={"date": DAY, "dry_run": False})
        ledger = (await c.get("/v1/deliveries", params={"date": DAY})).json()
        activity = (await c.get("/v1/activity", params={"date": DAY})).json()
    assert r1.json()["deliveries"] == 2 and r1.json()["events"] == 3
    assert {k: v for k, v in r2.json().items() if k not in ("date", "dry_run")} == {
        "deliveries": 0,
        "expected": 0,
        "reconciliations": 0,
        "events": 0,
        "media_dirs": 0,
        "kept_device_deliveries": 0,
        "kept_simulated": 0,
    }
    assert ledger["deliveries"] == [] and activity["events"] == []
    # The next day is untouched, files included.
    assert await count(engine, deliveries) == 1
    assert await count(engine, expected_deliveries) == 1
    assert [p.name for p in media.iterdir()] == [str(other)]
    assert not (media / str(a)).exists() and not (media / str(b)).exists()


async def test_device_deliveries_are_kept_and_reconciled_again(engine, media):
    await add_expected(engine, supplier="Sysco", carrier=None, count=1)
    device = await add_delivery(engine, media, T0)
    playground = await add_delivery(engine, media, T0 + timedelta(minutes=5))
    async with engine.begin() as conn:
        await conn.execute(
            update(deliveries).where(deliveries.c.id == device).values(source="ring_device")
        )
    await reconcile(engine, T0 + timedelta(hours=1))
    async with client_for(engine, media) as c:
        r = await c.post("/v1/admin/reset-day", json={"date": DAY, "dry_run": False})
    body = r.json()
    assert body["deliveries"] == 1 and body["kept_device_deliveries"] == 1
    async with engine.connect() as conn:
        left = (await conn.execute(select(deliveries.c.id))).scalars().all()
        recs = (await conn.execute(select(reconciliations.c.status))).scalars().all()
        events = (await conn.execute(select(func.count()).select_from(ring_events))).scalar_one()
    assert left == [device] and playground not in left
    # Its order was deleted with the day, so the kept delivery is now unexpected.
    assert recs == ["unexpected"]
    assert events == 1  # the device delivery's event stays


async def test_refuses_while_a_capture_is_in_flight(engine, media):
    await demo_day(engine, media)
    ev = await add_event(engine, T0 + timedelta(minutes=20), status="processing")
    async with client_for(engine, media) as c:
        r = await c.post("/v1/admin/reset-day", json={"date": DAY, "dry_run": False})
    assert r.status_code == 409 and "processing" in r.json()["detail"]
    assert await count(engine, deliveries) == 3
    async with engine.begin() as conn:
        await conn.execute(update(ring_events).where(ring_events.c.id == ev).values(status="done"))
    async with client_for(engine, media) as c:
        r = await c.post("/v1/admin/reset-day", json={"date": DAY, "dry_run": False})
    assert r.status_code == 200


async def test_disabled_unless_allowed_and_requires_admin(engine, media):
    await demo_day(engine, media)
    async with client_for(engine, media, allow=False) as c:
        off = await c.post("/v1/admin/reset-day", json={"dry_run": False})
    async with client_for(engine, media) as c:
        anon = await c.post(
            "/v1/admin/reset-day", json={"dry_run": False}, headers={"Authorization": ""}
        )
    assert (off.status_code, anon.status_code) == (404, 401)
    assert await count(engine, deliveries) == 3


async def test_media_outside_media_dir_is_never_removed(engine, media, tmp_path):
    a, _, _ = await demo_day(engine, media)
    # A same-named folder elsewhere must survive; only MEDIA_DIR/<id> is removed.
    elsewhere = tmp_path / "other" / str(a)
    elsewhere.mkdir(parents=True)
    async with client_for(engine, media) as c:
        await c.post("/v1/admin/reset-day", json={"date": DAY, "dry_run": False})
    assert elsewhere.is_dir()


# --- script --------------------------------------------------------------------------------


def test_script_dry_runs_by_default_and_explains_404():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if len(seen) == 2:
            return httpx.Response(404, json={"detail": "demo reset is disabled"})
        return httpx.Response(
            200,
            json={
                "date": "2026-10-17",
                "dry_run": True,
                "deliveries": 2,
                "expected": 2,
                "reconciliations": 2,
                "events": 3,
                "media_dirs": 2,
                "kept_device_deliveries": 1,
            },
        )

    client = httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))
    res = reset(client, "tok", None, dry_run=True)
    assert seen[0].headers["authorization"] == "Bearer tok"
    assert seen[0].url.path == "/v1/admin/reset-day"
    assert b'"dry_run":true' in seen[0].content.replace(b" ", b"")
    text = describe(res)
    assert "Would delete for 2026-10-17: 2 deliveries" in text
    assert "Kept 1 deliveries from a real Ring device." in text and "--yes" in text
    with pytest.raises(RuntimeError, match="ALLOW_DEMO_RESET"):
        reset(client, "tok", None, dry_run=False)
