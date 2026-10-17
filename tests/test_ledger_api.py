import contextlib
import json
import uuid
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import insert, update

from app.config import Settings
from app.db.models import deliveries, ring_events
from app.ledger import metrics
from app.main import create_app
from app.reconcile.cost import ReconcileConfig
from app.reconcile.store import business_day, reconcile_day
from tests.pipeline_fakes import T0, add_expected, tiny_jpeg

ADMIN = "admin-token"
AUTH = {"Authorization": f"Bearer {ADMIN}"}
LA = ZoneInfo("America/Los_Angeles")
DAY = business_day(T0, LA).isoformat()  # 2026-10-16
REPO = Path(__file__).resolve().parents[1]


@contextlib.asynccontextmanager
async def client_for(engine, media_dir: Path, results_root: Path = REPO):
    settings = Settings(
        _env_file=None, admin_token=ADMIN, media_dir=media_dir, results_root=results_root
    )
    app = create_app(settings, engine=engine, start_background=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH
        ) as c:
            yield c


async def add_event(engine, at, status="done", outcome=None) -> uuid.UUID:
    ev = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            insert(ring_events).values(
                id=ev,
                request_id=f"manual:{ev}",
                origin="manual",
                event_type="manual_trigger",
                occurred_at=at,
                received_at=at,
                raw={},
                relevant=True,
                status=status,
                outcome=outcome,
            )
        )
    return ev


async def add_delivery(engine, media_dir: Path, at, count=1, carrier="UPS", frames=3):
    """A delivery with evidence frames on disk, laid out as the pipeline writes them."""
    ev = await add_event(engine, at)
    did = uuid.uuid4()
    out = media_dir / str(did)
    out.mkdir(parents=True)
    paths = []
    for rank in range(frames):
        p = out / f"frame_{rank}_i{rank * 10:04d}_t{rank:05.2f}s.jpg"
        p.write_bytes(tiny_jpeg(rank))
        paths.append(str(p))
    async with engine.begin() as conn:
        await conn.execute(
            insert(deliveries).values(
                id=did,
                event_id=ev,
                device_id="ava1.ring.device.TEST",
                detected_at=at,
                carrier=carrier,
                package_count=count,
                label_text=["1Z999"],
                confidence=0.9,
                snapshot_path=paths[0] if paths else None,
                model_id="m",
                prompt_version="v1",
                frames_sampled=10,
                frames_sent=frames,
                latency_ms={"event_to_ledger": 12510.7},
                source="ring_playground",
            )
        )
    return did


async def reconcile(engine, now):
    await reconcile_day(engine, business_day(T0, LA), LA, now, ReconcileConfig())


@pytest.fixture
def media(tmp_path) -> Path:
    p = tmp_path / "media"
    p.mkdir()
    return p


async def test_all_read_endpoints_require_admin(engine, media):
    async with client_for(engine, media) as c:
        for path in (
            "/v1/deliveries",
            "/v1/expected",
            "/v1/discrepancies",
            "/v1/activity",
            "/v1/metrics",
            f"/v1/deliveries/{uuid.uuid4()}/snapshot",
        ):
            assert (await c.get(path, headers={"Authorization": "Bearer nope"})).status_code == 401
            assert (await c.get(path, headers={"Authorization": ""})).status_code == 401


async def test_day_ledger_with_statuses(engine, media):
    sysco = await add_expected(engine, supplier="Sysco", carrier="UPS", count=3)
    later = await add_expected(
        engine,
        supplier="US Foods",
        carrier=None,
        count=2,
        start=T0 + timedelta(hours=3),
        end=T0 + timedelta(hours=5),
    )
    short = await add_delivery(engine, media, T0, count=1)
    stray = await add_delivery(engine, media, T0 + timedelta(minutes=30), count=4, carrier="DHL")
    # Next day: must not appear.
    await add_delivery(engine, media, T0 + timedelta(days=1))
    await reconcile(engine, T0 + timedelta(hours=1))

    async with client_for(engine, media) as c:
        body = (await c.get("/v1/deliveries", params={"date": DAY})).json()
        exp = (await c.get("/v1/expected", params={"date": DAY})).json()

    assert body["date"] == DAY and body["timezone"] == "America/Los_Angeles"
    assert body["today"] != DAY  # T0 is in the past
    rows = body["deliveries"]
    assert [x["id"] for x in rows] == [str(stray), str(short)]  # newest first
    s = rows[1]
    assert s["status"] == "short" and "Short by 2" in s["reason"]
    assert s["expected"]["supplier"] == "Sysco" and s["expected"]["expected_count"] == 3
    assert s["event_to_ledger_ms"] == 12510.7 and s["label_text"] == ["1Z999"]
    assert s["media"] == {
        "snapshot": f"/v1/deliveries/{short}/snapshot",
        "frames": [f"/v1/deliveries/{short}/frames/{i}" for i in range(3)],
        "clip": None,
    }
    # DHL, 4 boxes: too far from either order to pair.
    assert rows[0]["status"] == "unexpected" and rows[0]["expected"] is None

    e = exp["expected"]
    assert [x["id"] for x in e] == [str(sysco), str(later)]  # window order
    assert e[0]["status"] == "short" and e[0]["delivery"]["id"] == str(short)
    # Window still open: no reconciliation row yet.
    assert e[1]["status"] == "pending" and e[1]["delivery"] is None and e[1]["reason"] is None


async def test_delivery_without_reconciliation_is_pending(engine, media):
    await add_delivery(engine, media, T0)
    async with client_for(engine, media) as c:
        rows = (await c.get("/v1/deliveries", params={"date": DAY})).json()["deliveries"]
    assert rows[0]["status"] == "pending" and rows[0]["expected"] is None


async def test_discrepancies_most_urgent_first(engine, media):
    await add_expected(engine, supplier="Sysco", carrier="UPS", count=3)
    await add_expected(
        engine,
        supplier="Cups Co",
        carrier="FedEx",
        count=5,
        start=T0 - timedelta(hours=4),
        end=T0 - timedelta(hours=3),
    )
    matched_order = await add_expected(
        engine, supplier="Bakery", carrier="Amazon", count=2, start=T0, end=T0 + timedelta(hours=1)
    )
    await add_delivery(engine, media, T0, count=1, carrier="UPS")  # short vs Sysco
    await add_delivery(engine, media, T0 + timedelta(minutes=5), count=2, carrier="Amazon")
    await add_delivery(engine, media, T0 + timedelta(minutes=30), count=4, carrier="DHL")
    await reconcile(engine, T0 + timedelta(hours=1))

    async with client_for(engine, media) as c:
        rows = (await c.get("/v1/discrepancies", params={"date": DAY})).json()["discrepancies"]

    assert [x["status"] for x in rows] == ["missing", "short", "unexpected"]
    missing, short, unexpected = rows
    assert missing["delivery"] is None and missing["expected"]["supplier"] == "Cups Co"
    assert short["delivery"]["package_count"] == 1 and short["expected"]["supplier"] == "Sysco"
    assert unexpected["expected"] is None and unexpected["delivery"]["carrier"] == "DHL"
    assert str(matched_order) not in json.dumps(rows)


async def test_activity_shows_in_flight_and_outcomes(engine, media):
    d = await add_delivery(engine, media, T0)
    await add_event(engine, T0 + timedelta(minutes=1), status="done", outcome="no packages")
    await add_event(engine, T0 + timedelta(minutes=2), status="processing")
    await add_event(engine, T0 + timedelta(days=1), status="pending")  # other day
    async with client_for(engine, media) as c:
        events = (await c.get("/v1/activity", params={"date": DAY})).json()["events"]
        limited = (await c.get("/v1/activity", params={"date": DAY, "limit": 1})).json()
    assert [x["status"] for x in events] == ["processing", "done", "done"]
    assert events[1]["outcome"] == "no packages" and events[1]["delivery_id"] is None
    assert events[2]["delivery_id"] == str(d)
    assert len(limited["events"]) == 1


async def test_default_date_is_today_in_business_timezone(engine, media):
    async with client_for(engine, media) as c:
        body = (await c.get("/v1/deliveries")).json()
    assert body["timezone"] == "America/Los_Angeles" and len(body["date"]) == 10
    assert body["today"] == body["date"]


async def test_bad_date_is_rejected(engine, media):
    async with client_for(engine, media) as c:
        assert (await c.get("/v1/deliveries", params={"date": "yesterday"})).status_code == 422


async def test_media_is_served_by_rank_and_confined_to_media_dir(engine, media, tmp_path):
    d = await add_delivery(engine, media, T0)
    no_frames = await add_delivery(engine, media, T0 + timedelta(minutes=1), frames=0)
    async with client_for(engine, media) as c:
        snap = await c.get(f"/v1/deliveries/{d}/snapshot")
        f2 = await c.get(f"/v1/deliveries/{d}/frames/2")
        f3 = await c.get(f"/v1/deliveries/{d}/frames/3")
        neg = await c.get(f"/v1/deliveries/{d}/frames/-1")
        clip = await c.get(f"/v1/deliveries/{d}/clip")
        none = await c.get(f"/v1/deliveries/{no_frames}/snapshot")
        unknown = await c.get(f"/v1/deliveries/{uuid.uuid4()}/snapshot")
    assert snap.status_code == 200 and snap.headers["content-type"] == "image/jpeg"
    assert snap.content == tiny_jpeg(0) and "immutable" in snap.headers["cache-control"]
    assert f2.status_code == 200 and f2.content == tiny_jpeg(2)
    assert (f3.status_code, neg.status_code, clip.status_code) == (404, 404, 404)
    assert (none.status_code, unknown.status_code) == (404, 404)

    # A stored path outside MEDIA_DIR is never served.
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"x")
    async with engine.begin() as conn:
        await conn.execute(
            update(deliveries).where(deliveries.c.id == d).values(snapshot_path=str(outside))
        )
    async with client_for(engine, media) as c:
        assert (await c.get(f"/v1/deliveries/{d}/snapshot")).status_code == 404


async def test_metrics_endpoint_reads_committed_results(engine, media):
    async with client_for(engine, media) as c:
        body = (await c.get("/v1/metrics")).json()
    assert body == json.loads(json.dumps(metrics.load(REPO), default=str))
    assert body["extraction"]["file"] == "eval/results/extraction.json"


# --- metrics module (no database) ---------------------------------------------------------


def test_metrics_copies_values_and_n_from_results_files():
    m = metrics.load(REPO)
    ext = json.loads((REPO / "eval/results/extraction.json").read_text())
    by_key = {(x["source"], x["model"]): x for x in ext["per_source"]}
    rows = m["extraction"]["rows"]
    assert len(rows) == len(by_key)
    for row in rows:
        src = by_key[(row["source"], row["model"])]
        assert row["n"] == src["n"]
        assert row["count_exact"]["k"] == src["count_exact"]["k"]
        assert row["count_exact"]["n"] == src["count_exact"]["n"]
        assert row["count_exact"]["value"] == src["count_exact"]["value"]
        # Carrier accuracy only where carrier truth exists.
        assert (row["carrier_accuracy"] is None) == (not src["carrier_accuracy"]["n"])
    assert rows[0]["source"] == "roboflow_porch_piracy"  # real footage first

    rec = json.loads((REPO / "eval/results/reconciliation.json").read_text())
    p = rec["pooled"]["discrepancy_micro"]["precision"]
    assert m["reconciliation"]["precision"] == {"value": p["value"], "k": p["k"], "n": p["n"]}
    assert m["reconciliation"]["data"].startswith("synthetic")

    lat = {x["file"]: x for x in m["latency"]}
    settle = lat["bench/results/latency_playground_manual_settle.json"]
    src = json.loads((REPO / "bench/results/latency_playground_manual_settle.json").read_text())
    assert settle["n_deliveries"] == src["n_deliveries"]
    assert settle["event_to_ledger"]["p50_ms"] == src["stages"]["event_to_ledger"]["p50_ms"]
    assert settle["label"] == "Scene-settled capture"


def test_metrics_missing_files_are_null_not_defaults(tmp_path):
    assert metrics.load(tmp_path) == {"extraction": None, "reconciliation": None, "latency": []}
    (tmp_path / "bench/results").mkdir(parents=True)
    (tmp_path / "bench/results/latency_broken.json").write_text("{not json")
    assert metrics.load(tmp_path)["latency"] == []
