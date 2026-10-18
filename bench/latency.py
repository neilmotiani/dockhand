"""Event → ledger latency, p50/p95 per pipeline stage, from `deliveries.latency_ms`.

Every delivery the pipeline stores carries its stage timings (app/pipeline.py). Stages, in ms:
ring_to_received (Ring detection → our receipt; includes the poll interval for polled events),
queue_wait, device_resolve, whep_setup, capture (whole WHEP session, setup included), select,
extract, save_frames, ledger_insert, reconcile, and the end-to-end spans event_to_ledger
(Ring detection → delivery row committed) and received_to_ledger.

Percentiles use the nearest-rank method, so every reported value is an observed one. N is
reported per stage, because some stages are missing for some events (manual triggers have no
Ring detection time). Filter by provenance with --source and by ingest path with --origin.
Simulated demo history (`source = simulated`, scripts/seed_demo_history.py) is always excluded.

Usage:
    uv run python -m bench.latency                                  # all deliveries
    uv run python -m bench.latency --source ring_playground --origin poll
    uv run python -m bench.latency --since 2026-10-16T20:30:00Z --until 2026-10-16T21:00:00Z
"""

import argparse
import asyncio
import json
import math
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db.models import LIVE_SOURCES, deliveries, ring_events
from app.db.session import make_engine

DEFAULT_OUT = Path("bench/results/latency.json")
STAGE_ORDER = [
    "event_to_ledger",
    "received_to_ledger",
    "ring_to_received",
    "queue_wait",
    "device_resolve",
    "whep_setup",
    "capture",
    "select",
    "extract",
    "save_frames",
    "ledger_insert",
    "reconcile",
]


def nearest_rank(values: list[float], p: float) -> float:
    """The smallest value with at least p% of observations at or below it."""
    if not values:
        raise ValueError("no values")
    s = sorted(values)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


def summarize(rows: list[dict]) -> dict[str, dict]:
    """rows: latency_ms dicts. Returns {stage: {n, p50, p95, max}} for every stage seen."""
    by_stage: dict[str, list[float]] = {}
    for row in rows:
        for stage, v in row.items():
            if isinstance(v, int | float) and not isinstance(v, bool):
                by_stage.setdefault(stage, []).append(float(v))
    order = STAGE_ORDER + sorted(set(by_stage) - set(STAGE_ORDER))
    return {
        s: {
            "n": len(by_stage[s]),
            "p50_ms": nearest_rank(by_stage[s], 50),
            "p95_ms": nearest_rank(by_stage[s], 95),
            "max_ms": max(by_stage[s]),
        }
        for s in order
        if s in by_stage
    }


async def load(
    database_url: str,
    source: str | None,
    origin: str | None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    engine = make_engine(database_url)
    stmt = select(deliveries.c.latency_ms).join(
        ring_events, ring_events.c.id == deliveries.c.event_id
    )
    # Simulated demo history never feeds a benchmark, whatever the filters.
    stmt = stmt.where(deliveries.c.source.in_(LIVE_SOURCES))
    if source:
        stmt = stmt.where(deliveries.c.source == source)
    if origin:
        stmt = stmt.where(ring_events.c.origin == origin)
    if since:
        stmt = stmt.where(deliveries.c.detected_at >= since)
    if until:
        stmt = stmt.where(deliveries.c.detected_at < until)
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(stmt)).scalars())
    finally:
        await engine.dispose()


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
        return out.stdout.strip() or None
    except OSError:
        return None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", choices=list(LIVE_SOURCES))
    p.add_argument("--origin", choices=["webhook", "poll", "manual"])
    p.add_argument("--since", type=datetime.fromisoformat, help="detected_at >= (ISO, with tz)")
    p.add_argument("--until", type=datetime.fromisoformat, help="detected_at < (ISO, with tz)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()

    for name in ("since", "until"):
        value = getattr(args, name)
        if value is not None and value.tzinfo is None:
            raise SystemExit(f"--{name} needs a timezone, e.g. 2026-10-16T20:30:00Z")
    rows = asyncio.run(
        load(get_settings().database_url, args.source, args.origin, args.since, args.until)
    )
    if not rows:
        raise SystemExit("no deliveries match; nothing to report")
    stages = summarize(rows)
    report = {
        "script": "bench/latency.py",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "filters": {
            "source": args.source,
            "origin": args.origin,
            "since": args.since.isoformat() if args.since else None,
            "until": args.until.isoformat() if args.until else None,
        },
        "n_deliveries": len(rows),
        "percentile_method": "nearest-rank",
        "stages": stages,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"N = {len(rows)} deliveries -> {args.out}")
    print(f"{'stage':20} {'n':>5} {'p50 ms':>10} {'p95 ms':>10}")
    for stage, v in stages.items():
        print(f"{stage:20} {v['n']:>5} {v['p50_ms']:>10.1f} {v['p95_ms']:>10.1f}")


if __name__ == "__main__":
    main()
