"""Seed today's expected deliveries through POST /v1/expected, for a live Playground test.

The `playground` scenario is built around what the Playground Package scene shows: one box on
the steps, no driver or truck, so the carrier is usually unreadable. It creates two orders
with no carrier (the matcher then charges the same "unknown carrier" cost for both):
- "Sysco" expecting 1 box   -> one Playground delivery pairs with it      (`matched`)
- "US Foods" expecting 3     -> the other pairs with it, 1 of 3           (`short`)

Two one-box deliveries are interchangeable, so which one gets which order is a tie-break
(in the 2026-10-16 live run the second delivery got Sysco). Run this, then trigger the
pipeline twice with different idempotency keys (POST /v1/trigger; the Playground produces
no motion events for the poller). With only one delivery so far, it pairs with Sysco and
US Foods stays pending until its window plus grace has passed.

IDs are uuid5(date, supplier) and the windows are fixed clock times on the given date, so
re-running on the same day reports `duplicate` instead of creating a second set. To seed a
second, separate set on the same day, pass `--run NAME`: it goes into the IDs and references,
and re-running with the same NAME is again a no-op. Give it a narrow window (--start/--end)
around the test, so the matcher pairs the new deliveries with the new orders: deliveries
outside a window cost more to pair with it.

Usage:
    uv run python -m scripts.seed_expected                      # today, 06:00–22:00 local
    uv run python -m scripts.seed_expected --date 2026-10-17 --start 08:00 --end 18:00
    uv run python -m scripts.seed_expected --run settle --start 17:40 --end 18:40
"""

import argparse
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings

SEED_NAMESPACE = uuid.UUID("5e3d9c1a-7b2f-4c8e-a6d4-9f0e1b2c3d4e")


@dataclass(frozen=True)
class Order:
    supplier: str
    expected_count: int
    reference: str


PLAYGROUND = [Order("Sysco", 1, "PO-DEMO-SYSCO"), Order("US Foods", 3, "PO-DEMO-USFOODS")]


def build_requests(
    day: date, tz: ZoneInfo, start: time, end: time, run: str | None = None
) -> list[dict]:
    window_start = datetime.combine(day, start, tzinfo=tz)
    window_end = datetime.combine(day, end, tzinfo=tz)
    if window_end < window_start:
        raise ValueError("--end must not be before --start")
    # No run: the original IDs, so earlier seeds stay idempotent.
    run_key = f"{run}|" if run else ""
    return [
        {
            "id": str(uuid.uuid5(SEED_NAMESPACE, f"{day.isoformat()}|{run_key}{o.supplier}")),
            "supplier": o.supplier,
            "carrier": None,
            "expected_count": o.expected_count,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "reference": f"{o.reference}-{day:%Y%m%d}" + (f"-{run}" if run else ""),
        }
        for o in PLAYGROUND
    ]


def seed(client: httpx.Client, admin_token: str, requests: list[dict]) -> list[dict]:
    out = []
    for body in requests:
        r = client.post(
            "/v1/expected", json=body, headers={"Authorization": f"Bearer {admin_token}"}
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"POST /v1/expected -> {r.status_code}: {r.text[:300]}")
        out.append(r.json())
    return out


def main() -> None:
    settings = get_settings()
    tz = ZoneInfo(settings.business_timezone)
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--api", default="http://localhost:8010")
    p.add_argument("--date", type=date.fromisoformat, default=datetime.now(tz).date())
    p.add_argument("--start", type=time.fromisoformat, default=time(6, 0))
    p.add_argument("--end", type=time.fromisoformat, default=time(22, 0))
    p.add_argument("--run", help="label for a separate set of orders on the same day")
    args = p.parse_args()
    if settings.admin_token is None:
        sys.exit("ADMIN_TOKEN is not set (in .env or the environment)")

    reqs = build_requests(args.date, tz, args.start, args.end, args.run)
    with httpx.Client(base_url=args.api, timeout=30) as client:
        results = seed(client, settings.admin_token.get_secret_value(), reqs)
    for res in results:
        e, rec = res["expected"], res["reconciliation"]
        state = f"{rec['status']}: {rec['reason']}" if rec else "pending (no delivery yet)"
        print(f"{res['status']:9} {e['supplier']:9} x{e['expected_count']}  {state}")


if __name__ == "__main__":
    main()
