"""Generate synthetic weeks of expected and detected deliveries with known ground truth.

The scenario is the café from SPEC.md: food-service trucks early in the morning, a local bakery
almost daily, and parcel carriers during the day. Two suppliers both ship via UPS on Wednesdays
with overlapping windows, so carrier alone cannot tell them apart.

Injected problems, per expected delivery (mutually exclusive outcomes):
  missing  - nothing arrives
  short    - fewer boxes than expected
  over     - more boxes than expected
and, independently, for anything that does arrive:
  late     - arrives 15 min to 4 h after the window closes
  early    - arrives 15 min to 2 h before the window opens
  mislabel - the detected carrier is wrong (another carrier, or Unknown), simulating a
             vision misread
Plus extras: deliveries nobody expected (USPS, DHL, stray parcels) on random days.

What this does NOT model: package-count misreads by the vision model, split deliveries (one
order arriving in two drops), or windows spanning midnight. Those would make the task harder.

All data is synthetic. Times are UTC-aware datetimes standing in for the café's local time.

Usage:
    uv run python -m eval.simulate --weeks 20 --seed 101 --out eval/results/sim_101.json
"""

import argparse
import json
import random
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from app.reconcile.models import DetectedDelivery, ExpectedDelivery, Status

CARRIERS = ["UPS", "FedEx", "USPS", "Amazon", "DHL", "Sysco", "US Foods", "Other"]


@dataclass(frozen=True)
class Supplier:
    name: str
    carrier: str
    weekdays: tuple[int, ...]  # Monday = 0
    window: tuple[time, time]
    count_range: tuple[int, int]
    probability: float = 1.0  # chance an order is placed on a scheduled day


CAFE_SUPPLIERS = [
    Supplier("Sysco", "Sysco", (0, 3), (time(6, 0), time(9, 0)), (4, 10)),
    Supplier("US Foods", "US Foods", (1, 4), (time(7, 0), time(10, 0)), (3, 8)),
    Supplier("Daybreak Bakery", "Other", (0, 1, 2, 3, 4, 5), (time(5, 30), time(7, 0)), (1, 3)),
    Supplier("Cup & Lid Supply", "UPS", (2,), (time(10, 0), time(14, 0)), (2, 6)),
    Supplier("Napkin Depot", "UPS", (2,), (time(10, 30), time(15, 0)), (1, 4), 0.5),
    Supplier("Ridgeline Roasters", "FedEx", (1,), (time(10, 0), time(16, 30)), (1, 4)),
    Supplier("Office supplies", "Amazon", (0, 2, 4), (time(9, 0), time(18, 0)), (1, 3), 0.5),
]

EXTRA_CARRIERS = (["USPS"] * 4) + (["DHL"] * 2) + ["FedEx", "UPS", "Amazon"]


@dataclass(frozen=True)
class Rates:
    missing: float = 0.06
    short: float = 0.08
    over: float = 0.04
    late: float = 0.10
    early: float = 0.03
    mislabel: float = 0.06
    extra_per_day: float = 0.30


DEFAULT_RATES = Rates()


@dataclass(frozen=True)
class TruthRow:
    expected_id: uuid.UUID | None
    delivery_id: uuid.UUID | None
    status: Status
    tags: tuple[str, ...] = ()  # injected problems, e.g. ("late", "mislabel")


@dataclass
class Day:
    day: date
    expected: list[ExpectedDelivery] = field(default_factory=list)
    deliveries: list[DetectedDelivery] = field(default_factory=list)
    truth: list[TruthRow] = field(default_factory=list)

    @property
    def reconcile_at(self) -> datetime:
        """End of the day: every window plus grace has closed by then."""
        return datetime.combine(self.day + timedelta(days=1), time(0, 0), UTC)


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, UTC)


def _rand_between(rng: random.Random, a: datetime, b: datetime) -> datetime:
    secs = int((b - a).total_seconds())
    return a + timedelta(seconds=rng.randint(0, secs))


def _uuid(rng: random.Random) -> uuid.UUID:
    return uuid.UUID(int=rng.getrandbits(128), version=4)


def simulate(
    weeks: int,
    seed: int,
    start: date = date(2026, 3, 2),  # a Monday
    rates: Rates = DEFAULT_RATES,
    suppliers: list[Supplier] = CAFE_SUPPLIERS,
) -> list[Day]:
    rng = random.Random(seed)
    days: list[Day] = []
    for offset in range(weeks * 7):
        d = start + timedelta(days=offset)
        day = Day(d)
        for s in suppliers:
            if d.weekday() not in s.weekdays or rng.random() >= s.probability:
                continue
            _simulate_order(rng, day, s, rates)
        if rng.random() < rates.extra_per_day:
            _simulate_extra(rng, day)
        days.append(day)
    return days


def _simulate_order(rng: random.Random, day: Day, s: Supplier, rates: Rates) -> None:
    ws, we = _at(day.day, s.window[0]), _at(day.day, s.window[1])
    n = rng.randint(*s.count_range)
    exp = ExpectedDelivery(
        id=_uuid(rng),
        supplier=s.name,
        carrier=s.carrier,
        expected_count=n,
        window_start=ws,
        window_end=we,
        reference=f"PO-{rng.randint(10000, 99999)}",
    )
    day.expected.append(exp)

    r = rng.random()
    if r < rates.missing:
        day.truth.append(TruthRow(exp.id, None, Status.MISSING, ("missing",)))
        return
    r -= rates.missing
    tags: list[str] = []
    if r < rates.short and n >= 2:
        count, status = rng.randint(1, n - 1), Status.SHORT
        tags.append("short")
    elif rates.short <= r < rates.short + rates.over:
        count, status = n + rng.randint(1, 3), Status.OVER
        tags.append("over")
    else:
        count, status = n, Status.MATCHED

    t = rng.random()
    if t < rates.late:
        at = we + timedelta(minutes=rng.randint(15, 240))
        tags.append("late")
    elif t < rates.late + rates.early:
        at = ws - timedelta(minutes=rng.randint(15, 120))
        tags.append("early")
    else:
        at = _rand_between(rng, ws, we)

    carrier = s.carrier
    if rng.random() < rates.mislabel:
        carrier = rng.choice([c for c in CARRIERS if c != s.carrier] + ["Unknown"])
        tags.append("mislabel")

    det = DetectedDelivery(id=_uuid(rng), detected_at=at, carrier=carrier, package_count=count)
    day.deliveries.append(det)
    day.truth.append(TruthRow(exp.id, det.id, status, tuple(tags)))


def _simulate_extra(rng: random.Random, day: Day) -> None:
    at = _rand_between(rng, _at(day.day, time(8, 0)), _at(day.day, time(18, 0)))
    det = DetectedDelivery(
        id=_uuid(rng),
        detected_at=at,
        carrier=rng.choice(EXTRA_CARRIERS),
        package_count=rng.randint(1, 3),
    )
    day.deliveries.append(det)
    day.truth.append(TruthRow(None, det.id, Status.UNEXPECTED, ("extra",)))


def to_json(days: list[Day]) -> list[dict]:
    def row(t: TruthRow) -> dict:
        out = asdict(t)
        out["expected_id"] = str(t.expected_id) if t.expected_id else None
        out["delivery_id"] = str(t.delivery_id) if t.delivery_id else None
        out["status"] = str(t.status)
        out["tags"] = list(t.tags)
        return out

    return [
        {
            "day": d.day.isoformat(),
            "expected": [e.model_dump(mode="json") for e in d.expected],
            "deliveries": [x.model_dump(mode="json") for x in d.deliveries],
            "truth": [row(t) for t in d.truth],
        }
        for d in days
    ]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--weeks", type=int, default=20)
    p.add_argument("--seed", type=int, default=101)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    days = simulate(args.weeks, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(to_json(days), indent=2) + "\n")
    n_exp = sum(len(d.expected) for d in days)
    n_del = sum(len(d.deliveries) for d in days)
    print(f"wrote {args.out}: {len(days)} days, {n_exp} expected, {n_del} detected deliveries")


if __name__ == "__main__":
    main()
