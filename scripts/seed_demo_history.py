"""Backfill the past 21 days, and today so far, with simulated deliveries for the dashboard.

Everything written here is synthetic and tagged as such: deliveries get `source = 'simulated'`
(never `ring_playground` or `ring_device`) and orders get `simulated = true`. The dashboard
badges these rows and can hide them, the reset endpoint keeps them, reconciliation matches
them apart from real rows, and no metric reads them (README, eval/results/, bench/results/ and
"How it works" all come from the committed eval and bench scripts).

Days come from eval/simulate.py, with five suppliers and lower problem rates than the eval
uses: most orders match, a handful are short, late or missing, and the odd unexpected parcel
turns up. US Foods is deliberately less reliable than the rest, so the Suppliers page has
something to point at. Each day is generated from its own seed (derived from --seed and the
date), so a given date always gets the same deliveries, whichever day the script runs on.

Today is filled up to the moment the script runs, so the Today page looks like a business day
in progress: deliveries detected before now, and orders whose window has started (or whose
delivery has already arrived). Orders still inside their window come out `pending`, as real
ones would. Rerunning later in the day adds what has "happened" since. Pass --no-today to
seed past days only.

Safe to rerun: one transaction deletes every simulated row (and its reconciliations and
snapshot folder) and inserts the fresh set, then each day is reconciled. Real rows are never
touched.

Snapshots reuse the synthetic composites from eval/build_synthetic.py (data/eval/synthetic/,
attribution in the README). Each delivery gets the composite closest to what was "seen"
(same carrier, nearest box count), copied into MEDIA_DIR/<delivery id>/. They illustrate the
row; the box count on the image need not equal the simulated count. Without the composites
the rows are written without snapshots.

Usage:
    uv run python -m scripts.seed_demo_history
    uv run python -m scripts.seed_demo_history --days 21 --seed 2027
    uv run python -m scripts.seed_demo_history --no-today
"""

import argparse
import asyncio
import hashlib
import json
import shutil
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import SIMULATED_SOURCE, get_settings
from app.db.models import deliveries, expected_deliveries, reconciliations
from app.db.session import init_db, make_engine
from app.ingest.queue import utcnow
from app.reconcile.cost import ReconcileConfig
from app.reconcile.store import ADVISORY_LOCK_KEY, business_day, reconcile_day
from eval.simulate import Day, Rates, Supplier, simulate

DEFAULT_DAYS = 21
# Chosen by looking at a handful of seeds for a mix with every kind of problem in it. This is
# demo data, not a measurement, so picking the seed is fine.
DEFAULT_SEED = 2027
COMPOSITES_DIR = Path("data/eval/synthetic")
COMPOSITE_ITEMS = Path("eval/labels/items_synthetic_overlay.jsonl")
SNAPSHOT_NAME = "frame_0_simulated.jpg"  # rank 0, as app/ledger/queries.py expects

# Most deliveries are fine. Per order: ~4% missing, ~5% short, ~7% late; plus an unexpected
# parcel on roughly one day in six.
DEMO_RATES = Rates(
    missing=0.04, short=0.05, over=0.02, late=0.07, early=0.02, mislabel=0.02, extra_per_day=0.15
)
# The supplier the Suppliers page should flag: often short, often late.
UNRELIABLE = Rates(
    missing=0.12, short=0.25, over=0.0, late=0.3, early=0.0, mislabel=0.0, extra_per_day=0.0
)

DEMO_SUPPLIERS = [
    Supplier("Sysco", "Sysco", (0, 3), (time(6, 0), time(9, 0)), (4, 10)),
    Supplier("US Foods", "US Foods", (1, 4), (time(7, 0), time(10, 0)), (3, 8), rates=UNRELIABLE),
    Supplier("Daybreak Bakery", "Other", (0, 1, 2, 3, 4, 5), (time(5, 30), time(7, 0)), (1, 3)),
    Supplier("Ridgeline Roasters", "FedEx", (1, 4), (time(10, 0), time(16, 0)), (1, 4)),
    Supplier("Amazon Business", "Amazon", (0, 2, 4), (time(9, 0), time(18, 0)), (1, 3), 0.6),
]


@dataclass
class SeedResult:
    days: int  # past days, today not included
    expected: int
    deliveries: int
    snapshots: int
    replaced_expected: int
    replaced_deliveries: int
    statuses: dict[str, int] = field(default_factory=dict)
    today_expected: int = 0
    today_deliveries: int = 0


def day_seed(seed: int, day: date) -> int:
    """A stable per-date seed, so the same date always simulates the same way."""
    return int.from_bytes(hashlib.sha256(f"{seed}|{day.isoformat()}".encode()).digest()[:8])


def simulate_days(first: date, n_days: int, seed: int) -> list[Day]:
    return [
        simulate(
            weeks=0,
            seed=day_seed(seed, d),
            start=d,
            rates=DEMO_RATES,
            suppliers=DEMO_SUPPLIERS,
            n_days=1,
        )[0]
        for d in (first + timedelta(days=i) for i in range(n_days))
    ]


def _local(t: datetime, tz: ZoneInfo) -> datetime:
    # eval/simulate.py writes local clock times as UTC-aware datetimes; pin them to the business
    # timezone instead.
    return t.replace(tzinfo=None).replace(tzinfo=tz)


def until(day: Day, cutoff: datetime, tz: ZoneInfo) -> Day:
    """The part of a simulated day that has happened by `cutoff`: deliveries detected before
    it, and orders whose window started before it or whose delivery is kept. Keeping the
    order of an early delivery avoids a made-up `unexpected` for a delivery whose order would
    otherwise be cut."""
    kept_d = {x.id for x in day.deliveries if _local(x.detected_at, tz) < cutoff}
    with_delivery = {t.expected_id for t in day.truth if t.delivery_id in kept_d}
    kept_e = {
        x.id for x in day.expected if _local(x.window_start, tz) < cutoff or x.id in with_delivery
    }
    return Day(
        day.day,
        expected=[x for x in day.expected if x.id in kept_e],
        deliveries=[x for x in day.deliveries if x.id in kept_d],
        truth=[
            t
            for t in day.truth
            if (t.expected_id is None or t.expected_id in kept_e)
            and (t.delivery_id is None or t.delivery_id in kept_d)
        ],
    )


@dataclass(frozen=True)
class Composite:
    path: Path
    carrier: str
    count: int


def load_composites(items_file: Path, data_root: Path) -> list[Composite]:
    """Synthetic composites that exist on disk, with their true carrier and box count."""
    if not items_file.is_file():
        return []
    out = []
    for line in items_file.read_text().splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        path = data_root / item["media"][0]
        if path.is_file():
            out.append(Composite(path, item["carrier"], item["package_count"]))
    return out


def pick_composite(
    composites: list[Composite], delivery_id: uuid.UUID, carrier: str, count: int
) -> Composite | None:
    """Same carrier if there is one (Other shows as an unlabelled box), then nearest box
    count. Ties are broken by the delivery ID, so reruns pick the same image."""
    if not composites:
        return None
    want = "Unknown" if carrier in ("Other", "Unknown") else carrier
    pool = [c for c in composites if c.carrier == want] or composites
    best = min(abs(c.count - count) for c in pool)
    near = sorted((c for c in pool if abs(c.count - count) == best), key=lambda c: c.path.name)
    return near[delivery_id.int % len(near)]


def _snapshot_path(media_dir: Path, delivery_id: uuid.UUID) -> Path:
    return media_dir / str(delivery_id) / SNAPSHOT_NAME


def _write_snapshot(media_dir: Path, delivery_id: uuid.UUID, src: Path) -> None:
    dst = _snapshot_path(media_dir, delivery_id)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _remove_media(media_dir: Path, ids: list[uuid.UUID]) -> None:
    root = media_dir.resolve()
    for did in ids:
        path = (media_dir / str(did)).resolve()
        if path.parent == root and path.is_dir():
            shutil.rmtree(path)


async def seed_history(
    engine: AsyncEngine,
    *,
    today: date,
    tz: ZoneInfo,
    media_dir: Path,
    now: datetime,
    cfg: ReconcileConfig,
    n_days: int = DEFAULT_DAYS,
    seed: int = DEFAULT_SEED,
    composites: list[Composite] | None = None,
    include_today: bool = True,
) -> SeedResult:
    """Replace all simulated rows with `n_days` simulated days ending yesterday, plus today
    up to `now` unless `include_today` is false."""
    days = simulate_days(today - timedelta(days=n_days), n_days, seed)
    past = len(days)
    if include_today:
        days.append(until(simulate_days(today, 1, seed)[0], now, tz))
    composites = composites or []

    exp_rows, del_rows, snapshots = [], [], {}
    for day in days:
        for x in day.expected:
            exp_rows.append(
                {
                    "id": x.id,
                    "supplier": x.supplier,
                    "carrier": x.carrier,
                    "expected_count": x.expected_count,
                    "window_start": _local(x.window_start, tz),
                    "window_end": _local(x.window_end, tz),
                    "reference": x.reference,
                    "simulated": True,
                }
            )
        for x in day.deliveries:
            comp = pick_composite(composites, x.id, x.carrier, x.package_count)
            if comp:
                snapshots[x.id] = comp.path
            del_rows.append(
                {
                    "id": x.id,
                    "event_id": None,
                    "device_id": None,
                    "detected_at": _local(x.detected_at, tz),
                    "carrier": x.carrier,
                    "package_count": x.package_count,
                    "label_text": [],
                    "snapshot_path": str(_snapshot_path(media_dir, x.id)) if comp else None,
                    "frames_sent": 1 if comp else 0,
                    "latency_ms": {},
                    "source": SIMULATED_SOURCE,
                }
            )

    d, e, r = deliveries.c, expected_deliveries.c, reconciliations.c
    async with engine.begin() as conn:
        await conn.execute(text("select pg_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
        old_d = list(
            (await conn.execute(select(d.id).where(d.source == SIMULATED_SOURCE))).scalars()
        )
        old_e = list((await conn.execute(select(e.id).where(e.simulated.is_(True)))).scalars())
        if old_d or old_e:
            await conn.execute(
                delete(reconciliations).where(
                    or_(r.delivery_id.in_(old_d), r.expected_id.in_(old_e))
                )
            )
            await conn.execute(delete(deliveries).where(d.id.in_(old_d)))
            await conn.execute(delete(expected_deliveries).where(e.id.in_(old_e)))
        if exp_rows:
            await conn.execute(insert(expected_deliveries), exp_rows)
        if del_rows:
            await conn.execute(insert(deliveries), del_rows)

    await asyncio.to_thread(_remove_media, media_dir, old_d)
    for did, src in snapshots.items():
        await asyncio.to_thread(_write_snapshot, media_dir, did, src)

    statuses: Counter[str] = Counter()
    for day in days:
        run = await reconcile_day(engine, day.day, tz, now, cfg)
        # The day's batch can include real rows too; count only the simulated decisions.
        sim_ids = {x.id for x in day.deliveries} | {x.id for x in day.expected}
        statuses.update(
            str(rec.status)
            for rec in run.result.reconciliations
            if rec.delivery_id in sim_ids or rec.expected_id in sim_ids
        )

    today_day = days[past] if include_today else None
    return SeedResult(
        days=past,
        today_expected=len(today_day.expected) if today_day else 0,
        today_deliveries=len(today_day.deliveries) if today_day else 0,
        expected=len(exp_rows),
        deliveries=len(del_rows),
        snapshots=len(snapshots),
        replaced_expected=len(old_e),
        replaced_deliveries=len(old_d),
        statuses=dict(sorted(statuses.items())),
    )


async def _main(n_days: int, seed: int, include_today: bool) -> SeedResult:
    settings = get_settings()
    tz = ZoneInfo(settings.business_timezone)
    now = utcnow()
    composites = load_composites(COMPOSITE_ITEMS, Path("data"))
    if not composites:
        print(
            f"note: no synthetic composites in {COMPOSITES_DIR}; seeding without snapshots "
            "(build them with `uv run python -m eval.build_synthetic`)"
        )
    engine = make_engine(settings.database_url)
    try:
        await init_db(engine)
        return await seed_history(
            engine,
            today=business_day(now, tz),
            tz=tz,
            media_dir=Path(settings.media_dir),
            now=now,
            cfg=ReconcileConfig.from_settings(settings),
            n_days=n_days,
            seed=seed,
            composites=composites,
            include_today=include_today,
        )
    finally:
        await engine.dispose()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--days", type=int, default=DEFAULT_DAYS, help="days before today to fill")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument(
        "--no-today", dest="today", action="store_false", help="past days only, nothing today"
    )
    args = p.parse_args()
    if args.days < 1:
        p.error("--days must be at least 1")
    res = asyncio.run(_main(args.days, args.seed, args.today))
    if res.replaced_deliveries or res.replaced_expected:
        print(
            f"Replaced {res.replaced_expected} simulated orders and "
            f"{res.replaced_deliveries} simulated deliveries."
        )
    print(
        f"Seeded {res.days} past days and {'today so far' if args.today else 'nothing today'}: "
        f"{res.expected} orders, {res.deliveries} deliveries ({res.snapshots} with a snapshot)."
    )
    if args.today:
        e, d = res.today_expected, res.today_deliveries
        print(
            f"Today so far: {e} order{'' if e == 1 else 's'}, "
            f"{d} deliver{'y' if d == 1 else 'ies'}."
        )
    print("Statuses: " + ", ".join(f"{k} {v}" for k, v in res.statuses.items()))


if __name__ == "__main__":
    main()
