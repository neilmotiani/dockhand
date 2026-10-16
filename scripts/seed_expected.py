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
re-running on the same day reports `duplicate` instead of creating a second set.

Usage:
    uv run python -m scripts.seed_expected                      # today, 06:00–22:00 local
    uv run python -m scripts.seed_expected --date 2026-10-17 --start 08:00 --end 18:00
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


def build_requests(day: date, tz: ZoneInfo, start: time, end: time) -> list[dict]:
    window_start = datetime.combine(day, start, tzinfo=tz)
    window_end = datetime.combine(day, end, tzinfo=tz)
    if window_end < window_start:
        raise ValueError("--end must not be before --start")
    return [
        {
            "id": str(uuid.uuid5(SEED_NAMESPACE, f"{day.isoformat()}|{o.supplier}")),
            "supplier": o.supplier,
            "carrier": None,
            "expected_count": o.expected_count,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "reference": f"{o.reference}-{day:%Y%m%d}",
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
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--date", type=date.fromisoformat, default=datetime.now(tz).date())
    p.add_argument("--start", type=time.fromisoformat, default=time(6, 0))
    p.add_argument("--end", type=time.fromisoformat, default=time(22, 0))
    args = p.parse_args()
    if settings.admin_token is None:
        sys.exit("ADMIN_TOKEN is not set (in .env or the environment)")

    reqs = build_requests(args.date, tz, args.start, args.end)
    with httpx.Client(base_url=args.api, timeout=30) as client:
        results = seed(client, settings.admin_token.get_secret_value(), reqs)
    for res in results:
        e, rec = res["expected"], res["reconciliation"]
        state = f"{rec['status']}: {rec['reason']}" if rec else "pending (no delivery yet)"
        print(f"{res['status']:9} {e['supplier']:9} x{e['expected_count']}  {state}")


if __name__ == "__main__":
    main()
