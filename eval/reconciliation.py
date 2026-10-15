"""Reconciliation precision/recall on simulated weeks (eval/simulate.py).

Each simulated day is reconciled at end of day, exactly as the timer would in production, and
the predicted rows are compared with the simulator's ground truth. A predicted row counts as
correct only if the same (expected_id, delivery_id, status) row exists in the truth, so pairing
a delivery with the wrong order is an error even if the status happens to agree.

Reported per status and micro-averaged over the discrepancy statuses (short, over, missing,
unexpected). Every proportion comes with its N and a 95% Wilson interval.

Seed 7 was used while choosing the cost weights; reported numbers use other seeds.

Usage:
    uv run python -m eval.reconciliation                       # default seeds, writes results
    uv run python -m eval.reconciliation --seeds 1 2 --weeks 4 --out /tmp/r.json
"""

import argparse
import json
import math
import subprocess
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from app.reconcile import DISCREPANCY_STATUSES, ReconcileConfig, Status, reconcile
from eval.simulate import DEFAULT_RATES, Day, simulate

DEV_SEED = 7
DEFAULT_SEEDS = [101, 102, 103, 104, 105]
DEFAULT_WEEKS = 20
DEFAULT_OUT = Path("eval/results/reconciliation.json")
MIN_DELIVERIES = 200

Key = tuple[str | None, str | None, str]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(centre - half, 4), round(centre + half, 4))


def ratio(k: int, n: int) -> dict:
    return {"value": round(k / n, 4) if n else None, "k": k, "n": n, "ci95": wilson(k, n)}


def _key(expected_id, delivery_id, status) -> Key:
    return (
        str(expected_id) if expected_id else None,
        str(delivery_id) if delivery_id else None,
        str(status),
    )


def evaluate_days(days: list[Day], cfg: ReconcileConfig) -> dict:
    """Return raw counts for a list of days; `summarize` turns them into metrics."""
    pred_by_status: Counter[str] = Counter()
    truth_by_status: Counter[str] = Counter()
    tp_by_status: Counter[str] = Counter()
    tag_total: Counter[str] = Counter()
    tag_hit: Counter[str] = Counter()
    pairs_total = pairs_hit = 0
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    n_expected = n_deliveries = 0

    for day in days:
        n_expected += len(day.expected)
        n_deliveries += len(day.deliveries)
        result = reconcile(day.deliveries, day.expected, day.reconcile_at, cfg)
        pred = {_key(r.expected_id, r.delivery_id, r.status) for r in result.reconciliations}
        truth = {_key(t.expected_id, t.delivery_id, t.status): t for t in day.truth}

        for k in pred:
            pred_by_status[k[2]] += 1
        for k, t in truth.items():
            truth_by_status[k[2]] += 1
            hit = k in pred
            tp_by_status[k[2]] += hit
            for tag in t.tags:
                tag_total[tag] += 1
                tag_hit[tag] += hit
            if t.expected_id and t.delivery_id:
                pairs_total += 1
                pairs_hit += any(p[0] == k[0] and p[1] == k[1] for p in pred)

        pred_status_by_expected = {
            str(r.expected_id): str(r.status) for r in result.reconciliations if r.expected_id
        }
        for e_id in result.pending_expected_ids:
            pred_status_by_expected[str(e_id)] = "pending"
        for t in day.truth:
            if t.expected_id:
                confusion[str(t.status)][pred_status_by_expected[str(t.expected_id)]] += 1

    return {
        "n_days": len(days),
        "n_expected": n_expected,
        "n_deliveries": n_deliveries,
        "pred_by_status": dict(pred_by_status),
        "truth_by_status": dict(truth_by_status),
        "tp_by_status": dict(tp_by_status),
        "tag_total": dict(tag_total),
        "tag_hit": dict(tag_hit),
        "pairs_total": pairs_total,
        "pairs_hit": pairs_hit,
        "confusion": {k: dict(v) for k, v in confusion.items()},
    }


def merge_counts(parts: list[dict]) -> dict:
    out: dict = {}
    for part in parts:
        for k, v in part.items():
            if isinstance(v, int):
                out[k] = out.get(k, 0) + v
            elif k == "confusion":
                acc = out.setdefault(k, {})
                for row, cols in v.items():
                    for col, n in cols.items():
                        acc.setdefault(row, {})
                        acc[row][col] = acc[row].get(col, 0) + n
            else:
                acc = out.setdefault(k, {})
                for kk, n in v.items():
                    acc[kk] = acc.get(kk, 0) + n
    return out


def summarize(c: dict) -> dict:
    per_status = {}
    for s in Status:
        tp = c["tp_by_status"].get(s, 0)
        per_status[str(s)] = {
            "precision": ratio(tp, c["pred_by_status"].get(s, 0)),
            "recall": ratio(tp, c["truth_by_status"].get(s, 0)),
        }
    disc = [str(s) for s in DISCREPANCY_STATUSES]
    tp = sum(c["tp_by_status"].get(s, 0) for s in disc)
    n_pred = sum(c["pred_by_status"].get(s, 0) for s in disc)
    n_truth = sum(c["truth_by_status"].get(s, 0) for s in disc)
    return {
        "n_days": c["n_days"],
        "n_expected": c["n_expected"],
        "n_deliveries": c["n_deliveries"],
        "discrepancy_micro": {
            "precision": ratio(tp, n_pred),
            "recall": ratio(tp, n_truth),
        },
        "per_status": per_status,
        "pair_accuracy": ratio(c["pairs_hit"], c["pairs_total"]),
        "recall_by_injected_problem": {
            tag: ratio(c["tag_hit"].get(tag, 0), n) for tag, n in sorted(c["tag_total"].items())
        },
        "confusion_expected_truth_vs_pred": c["confusion"],
    }


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def run(seeds: list[int], weeks: int, cfg: ReconcileConfig) -> dict:
    per_seed = {}
    counts = []
    for seed in seeds:
        c = evaluate_days(simulate(weeks, seed), cfg)
        counts.append(c)
        per_seed[str(seed)] = summarize(c)
    cfg_json = asdict(cfg)
    cfg_json["grace_minutes"] = cfg.grace.total_seconds() / 60
    del cfg_json["grace"]
    return {
        "script": "eval/reconciliation.py",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "data": "synthetic (eval/simulate.py); not Ring footage",
        "seeds": seeds,
        "dev_seed_excluded": DEV_SEED,
        "weeks_per_seed": weeks,
        "injection_rates": asdict(DEFAULT_RATES),
        "config": cfg_json,
        "pooled": summarize(merge_counts(counts)),
        "per_seed": per_seed,
        "caveats": [
            "Simulator and matcher share assumptions (same cost features), so this measures the "
            "matcher on the problems we chose to inject, not real-world accuracy.",
            "Vision package-count misreads are not simulated; carrier misreads are (mislabel).",
            "Split deliveries and multi-day windows are not simulated.",
        ],
    }


def _fmt(r: dict) -> str:
    if r["value"] is None:
        return "n/a (N=0)"
    lo, hi = r["ci95"]
    return f"{r['value']:.3f} ({r['k']}/{r['n']}, 95% CI {lo:.3f}-{hi:.3f})"


def print_report(res: dict) -> None:
    p = res["pooled"]
    print(
        f"seeds={res['seeds']} weeks/seed={res['weeks_per_seed']} days={p['n_days']} "
        f"expected={p['n_expected']} deliveries={p['n_deliveries']}"
    )
    print(f"discrepancy precision  {_fmt(p['discrepancy_micro']['precision'])}")
    print(f"discrepancy recall     {_fmt(p['discrepancy_micro']['recall'])}")
    print(f"pair accuracy          {_fmt(p['pair_accuracy'])}")
    for s, m in p["per_status"].items():
        print(f"  {s:<11} P {_fmt(m['precision'])}")
        print(f"  {'':<11} R {_fmt(m['recall'])}")
    print("recall by injected problem:")
    for tag, r in p["recall_by_injected_problem"].items():
        print(f"  {tag:<9} {_fmt(r)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--seeds", type=int, nargs="+", default=DEFAULT_SEEDS)
    ap.add_argument("--weeks", type=int, default=DEFAULT_WEEKS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    res = run(args.seeds, args.weeks, ReconcileConfig())
    if res["pooled"]["n_deliveries"] < MIN_DELIVERIES:
        print(f"warning: only {res['pooled']['n_deliveries']} deliveries (< {MIN_DELIVERIES})")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2) + "\n")
    print_report(res)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
