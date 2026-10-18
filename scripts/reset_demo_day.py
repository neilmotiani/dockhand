"""Reset a demo day through POST /v1/admin/reset-day, to record a clean take.

Clears the day's Ring Playground deliveries (and their evidence frames), every real expected
delivery whose window starts that day, and the day's pipeline events. Deliveries from a real
Ring device and simulated history (scripts/seed_demo_history.py) are never deleted. See
app/ledger/reset.py.

Admin-only twice over: it needs ADMIN_TOKEN, and the API refuses unless it runs with
ALLOW_DEMO_RESET=true. Without --yes it only prints what would be deleted.

Usage:
    uv run python -m scripts.reset_demo_day                 # dry run, today
    uv run python -m scripts.reset_demo_day --yes           # delete today's demo data
    uv run python -m scripts.reset_demo_day --date 2026-10-17 --yes
    uv run python -m scripts.seed_expected                  # then seed fresh orders
"""

import argparse
import sys
from datetime import date

import httpx

from app.config import get_settings


def reset(client: httpx.Client, admin_token: str, day: date | None, dry_run: bool) -> dict:
    r = client.post(
        "/v1/admin/reset-day",
        json={"date": day.isoformat() if day else None, "dry_run": dry_run},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    if r.status_code == 404:
        raise RuntimeError(
            "reset is disabled: set ALLOW_DEMO_RESET=true in the API's .env and restart it"
        )
    if r.status_code != 200:
        raise RuntimeError(f"POST /v1/admin/reset-day -> {r.status_code}: {r.text[:300]}")
    return r.json()


def describe(res: dict) -> str:
    verb = "Would delete" if res["dry_run"] else "Deleted"
    lines = [
        f"{verb} for {res['date']}: {res['deliveries']} deliveries "
        f"({res['media_dirs']} evidence folders), {res['expected']} expected deliveries, "
        f"{res['reconciliations']} reconciliations, {res['events']} pipeline events.",
    ]
    if res["kept_device_deliveries"]:
        lines.append(f"Kept {res['kept_device_deliveries']} deliveries from a real Ring device.")
    if res.get("kept_simulated"):
        lines.append(f"Kept {res['kept_simulated']} simulated history rows.")
    if res["dry_run"]:
        lines.append("Dry run: nothing was deleted. Re-run with --yes to delete.")
    return "\n".join(lines)


def main() -> None:
    settings = get_settings()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--api", default="http://localhost:8010")
    p.add_argument("--date", type=date.fromisoformat, help="business day; default today")
    p.add_argument("--yes", action="store_true", help="actually delete (default: dry run)")
    args = p.parse_args()
    if settings.admin_token is None:
        sys.exit("ADMIN_TOKEN is not set (in .env or the environment)")
    with httpx.Client(base_url=args.api, timeout=30) as client:
        try:
            res = reset(client, settings.admin_token.get_secret_value(), args.date, not args.yes)
        except RuntimeError as e:
            sys.exit(str(e))
    print(describe(res))


if __name__ == "__main__":
    main()
