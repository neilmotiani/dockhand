import contextlib
import uuid
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import expected_deliveries
from app.main import create_app
from scripts.seed_expected import build_requests, seed
from tests.pipeline_fakes import T0
from tests.test_reconcile_store import add_delivery

ADMIN = "admin-token"
AUTH = {"Authorization": f"Bearer {ADMIN}"}
LA = ZoneInfo("America/Los_Angeles")


@contextlib.asynccontextmanager
async def client_for(engine, admin: str | None = ADMIN):
    app = create_app(
        Settings(_env_file=None, admin_token=admin), engine=engine, start_background=False
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c


def order(**over) -> dict:
    body = {
        "supplier": "Sysco",
        "carrier": "UPS",
        "expected_count": 2,
        "window_start": (T0 - timedelta(hours=1)).isoformat(),
        "window_end": (T0 + timedelta(hours=1)).isoformat(),
        "reference": "PO-1",
    }
    body.update(over)
    return body


async def n_expected(engine) -> int:
    async with engine.connect() as conn:
        stmt = select(func.count()).select_from(expected_deliveries)
        return (await conn.execute(stmt)).scalar_one()


async def test_requires_admin_and_is_disabled_without_token(engine):
    async with client_for(engine) as c:
        assert (await c.post("/v1/expected", json=order())).status_code == 401
    async with client_for(engine, admin=None) as c:
        assert (await c.post("/v1/expected", json=order(), headers=AUTH)).status_code == 404
    assert await n_expected(engine) == 0


async def test_create_reconciles_against_existing_delivery(engine):
    await add_delivery(engine, T0, count=1, carrier="UPS")
    async with client_for(engine) as c:
        r = await c.post("/v1/expected", json=order(expected_count=3), headers=AUTH)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "created" and body["expected"]["carrier"] == "UPS"
    assert body["reconciliation"]["status"] == "short"
    assert "Short by 2" in body["reconciliation"]["reason"]


async def test_pending_when_nothing_arrived_yet(engine):
    future = T0 + timedelta(days=3650)
    async with client_for(engine) as c:
        r = await c.post(
            "/v1/expected",
            json=order(
                window_start=future.isoformat(),
                window_end=(future + timedelta(hours=2)).isoformat(),
            ),
            headers=AUTH,
        )
    assert r.status_code == 201 and r.json()["reconciliation"] is None


async def test_same_id_is_idempotent_and_conflicting_fields_are_rejected(engine):
    oid = str(uuid.uuid4())
    async with client_for(engine) as c:
        r1 = await c.post("/v1/expected", json=order(id=oid), headers=AUTH)
        r2 = await c.post("/v1/expected", json=order(id=oid), headers=AUTH)
        # Same instant written with another UTC offset is the same order.
        same = order(
            id=oid,
            window_start=(T0 - timedelta(hours=1)).astimezone(LA).isoformat(),
            window_end=(T0 + timedelta(hours=1)).astimezone(LA).isoformat(),
        )
        r3 = await c.post("/v1/expected", json=same, headers=AUTH)
        r4 = await c.post("/v1/expected", json=order(id=oid, expected_count=5), headers=AUTH)
    assert (r1.status_code, r2.status_code, r3.status_code) == (201, 200, 200)
    assert r2.json()["status"] == "duplicate"
    assert r4.status_code == 409
    assert await n_expected(engine) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"expected_count": 0},
        {"supplier": ""},
        {"carrier": "Royal Mail"},
        {"window_start": "2026-10-16T10:00:00"},  # naive: ambiguous across timezones
        {
            "window_start": (T0 + timedelta(hours=2)).isoformat(),
            "window_end": T0.isoformat(),
        },
    ],
)
async def test_validation(engine, bad):
    async with client_for(engine) as c:
        r = await c.post("/v1/expected", json=order(**bad), headers=AUTH)
    assert r.status_code == 422
    assert await n_expected(engine) == 0


# --- seed script --------------------------------------------------------------------------


def test_seed_requests_are_deterministic_per_day():
    day = date(2026, 10, 16)
    a = build_requests(day, LA, time(6), time(22))
    b = build_requests(day, LA, time(6), time(22))
    assert a == b and len(a) == 2
    assert [(r["supplier"], r["expected_count"]) for r in a] == [("Sysco", 1), ("US Foods", 3)]
    assert all(r["carrier"] is None for r in a)
    assert a[0]["window_start"] == "2026-10-16T06:00:00-07:00"
    other = build_requests(date(2026, 10, 17), LA, time(6), time(22))
    assert {r["id"] for r in a}.isdisjoint(r["id"] for r in other)
    with pytest.raises(ValueError):
        build_requests(day, LA, time(22), time(6))


async def test_seed_scenario_yields_matched_and_short(engine):
    """Two one-box Playground deliveries (carrier unreadable) against the seeded orders."""
    day = T0.astimezone(LA).date()
    await add_delivery(engine, T0, count=1, carrier="Unknown")
    await add_delivery(engine, T0 + timedelta(minutes=10), count=1, carrier="Unknown")
    reqs = build_requests(day, LA, time(6), time(22))

    app = create_app(
        Settings(_env_file=None, admin_token=ADMIN), engine=engine, start_background=False
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            # seed() is synchronous (it's a CLI); drive the same requests through the app.
            results = [
                (await c.post("/v1/expected", json=body, headers=AUTH)).json() for body in reqs
            ]
            again = [
                (await c.post("/v1/expected", json=body, headers=AUTH)).json() for body in reqs
            ]
    statuses = {r["expected"]["supplier"]: r["reconciliation"]["status"] for r in again}
    assert statuses == {"Sysco": "matched", "US Foods": "short"}
    assert [r["status"] for r in results] == ["created", "created"]
    assert [r["status"] for r in again] == ["duplicate", "duplicate"]
    assert await n_expected(engine) == 2


def test_seed_posts_with_admin_token_and_raises_on_error():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if len(seen) == 2:
            return httpx.Response(401, json={"detail": "invalid admin token"})
        return httpx.Response(201, json={"status": "created"})

    client = httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))
    reqs = build_requests(date(2026, 10, 16), LA, time(6), time(22))
    with pytest.raises(RuntimeError, match="401"):
        seed(client, "tok", reqs)
    assert seen[0].headers["authorization"] == "Bearer tok"
    assert seen[0].url.path == "/v1/expected"
