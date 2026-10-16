"""Extraction accuracy, latency and cost on the labelled set, for the default and comparison models.

Items come from eval/labels/items_*.jsonl, one file per source (rules: eval/SELECTION.md).
Every item is sent to both models configured in app/config.py (BEDROCK_MODEL_ID and
BEDROCK_COMPARISON_MODEL_ID, each with its own structured-output setting). Results are reported
per source and model, never pooled across sources:

- package_count exact match (k/N, 95% Wilson interval) and mean absolute error;
- false alarms on negatives (predicted > 0 when truth is 0);
- carrier accuracy, only on items that have carrier truth;
- latency p50/p95 (wall clock per call, including a retry) and cost at eval/prices.json rates.

A failed extraction (invalid output after the retry, or an API error) counts as wrong and is
listed separately.

Spend control: each (model, prompt version, output mode, media) result is cached under
data/eval/cache/, so re-runs only pay for new calls. Every paid call is appended to
data/eval/spend.jsonl. Before calling anything, the script estimates the cost of the uncached
calls and stops if the ledger total plus the estimate would exceed --max-usd.

Usage:
    uv run python -m eval.extraction --dry-run        # estimate only, no model calls
    uv run python -m eval.extraction                  # run, write eval/results/extraction.json
"""

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from botocore.exceptions import ClientError

from app.config import Settings, get_settings
from app.extract.client import BedrockExtractor, ExtractionError, make_bedrock_client
from eval.reconciliation import ratio

LABELS = Path("eval/labels")
PRICES = Path("eval/prices.json")
DATA = Path("data")
CACHE = Path("data/eval/cache")
LEDGER = Path("data/eval/spend.jsonl")
DEFAULT_OUT = Path("eval/results/extraction.json")
FORMATS = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp"}

# Upper-bound token estimates for the budget check, from the 2026-10-15 smoke test
# (3 frames at 1280x720: Nova 6,733 input tokens without structured output, Sonnet 4,175 with it)
# plus headroom for larger images. They only gate spending; reported costs use real usage.
EST_TOKENS = {
    "per_image": {"amazon": 2500, "anthropic": 1900},
    "prompt": {"amazon": 1000, "anthropic": 700},
    "output": 200,
}
RETRY_FACTOR = 1.15  # allowance for the one retry on invalid output


REQUIRED = {"id", "source", "provenance", "media", "package_count", "carrier"}


def load_items(labels: Path = LABELS) -> list[dict]:
    items = []
    for f in sorted(labels.glob("items_*.jsonl")):
        items += [json.loads(line) for line in f.open() if line.strip()]
    ids = [i["id"] for i in items]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate item ids in {labels}")
    for it in items:
        missing = REQUIRED - it.keys()
        if missing:
            raise ValueError(f"{it.get('id')}: missing {sorted(missing)}")
    return items


def model_settings(base: Settings) -> list[Settings]:
    return [
        base,
        base.model_copy(
            update={
                "bedrock_model_id": base.bedrock_comparison_model_id,
                "bedrock_structured_output": base.bedrock_comparison_structured_output,
            }
        ),
    ]


def media_hash(item: dict) -> str:
    h = hashlib.sha256()
    for rel in item["media"]:
        h.update(hashlib.sha256((DATA / rel).read_bytes()).digest())
    return h.hexdigest()


def cache_path(s: Settings, digest: str) -> Path:
    mode = "structured" if s.bedrock_structured_output else "prompted"
    model = s.bedrock_model_id.replace(":", "_")
    return CACHE / model / s.extraction_prompt_version / mode / f"{digest}.json"


def vendor(model_id: str) -> str:
    return "anthropic" if ".anthropic." in model_id else "amazon"


def cost(prices: dict, model_id: str, inp: int, out: int) -> float:
    p = prices[model_id]
    return inp * p["input_per_mtok"] / 1e6 + out * p["output_per_mtok"] / 1e6


def estimate(prices: dict, model_id: str, n_images: int) -> float:
    v = vendor(model_id)
    inp = EST_TOKENS["prompt"][v] + n_images * EST_TOKENS["per_image"][v]
    return cost(prices, model_id, inp, EST_TOKENS["output"]) * RETRY_FACTOR


def ledger_total() -> float:
    if not LEDGER.exists():
        return 0.0
    return sum(json.loads(line)["usd"] for line in LEDGER.open() if line.strip())


def call(extractor: BedrockExtractor, item: dict, prices: dict) -> dict:
    images = [
        ((DATA / rel).read_bytes(), FORMATS[Path(rel).suffix.lower()]) for rel in item["media"]
    ]
    start = time.perf_counter()
    try:
        out = extractor.extract(images)
    except (ExtractionError, ClientError) as e:
        # A failed call may still have cost tokens; the extractor does not expose usage on
        # failure, so it is logged as unknown rather than zero.
        return {
            "ok": False,
            "error": str(e)[:300],
            "latency_ms": round((time.perf_counter() - start) * 1000, 1),
            "usd": None,
        }
    usd = cost(prices, extractor.model_id, out.usage.input_tokens, out.usage.output_tokens)
    return {
        "ok": True,
        "result": out.result.model_dump(mode="json"),
        "attempts": out.attempts,
        "input_tokens": out.usage.input_tokens,
        "output_tokens": out.usage.output_tokens,
        "latency_ms": out.latency_ms,
        "bedrock_latency_ms": out.bedrock_latency_ms,
        "structured_output": out.structured_output,
        "usd": round(usd, 6),
    }


def percentile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 1)


def summarize(rows: list[dict]) -> dict:
    """rows: [{item, pred}] for one (source, model)."""
    n = len(rows)
    exact = 0
    abs_err: list[int] = []
    neg_n = neg_fa = 0
    car_n = car_ok = 0
    failures = 0
    lat = [r["pred"]["latency_ms"] for r in rows]
    usd = [r["pred"]["usd"] for r in rows if r["pred"]["usd"] is not None]
    for r in rows:
        truth, pred = r["item"], r["pred"]
        if not pred["ok"]:
            failures += 1
            if truth["package_count"] == 0:
                neg_n += 1
            if truth.get("carrier") is not None:
                car_n += 1
            continue
        got = pred["result"]["package_count"]
        exact += got == truth["package_count"]
        abs_err.append(abs(got - truth["package_count"]))
        if truth["package_count"] == 0:
            neg_n += 1
            neg_fa += got > 0
        if truth.get("carrier") is not None:
            car_n += 1
            car_ok += pred["result"]["carrier"] == truth["carrier"]
    return {
        "n": n,
        "count_exact": ratio(exact, n),
        "count_mae": round(statistics.mean(abs_err), 3) if abs_err else None,
        "count_mae_n": len(abs_err),
        "count_wrong": n - exact,
        "negatives_false_alarm": ratio(neg_fa, neg_n),
        "carrier_accuracy": ratio(car_ok, car_n),
        "failures": failures,
        "latency_ms_p50": percentile(lat, 0.5),
        "latency_ms_p95": percentile(lat, 0.95),
        "usd_total": round(sum(usd), 6),
        "usd_per_call": round(sum(usd) / len(usd), 6) if usd else None,
    }


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="estimate cost, call nothing")
    p.add_argument("--max-usd", type=float, default=4.90, help="cap on total ledger spend")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()

    items = load_items()
    prices = json.loads(PRICES.read_text())
    models = model_settings(get_settings())
    for s in models:
        if s.bedrock_model_id not in prices:
            sys.exit(f"no verified price for {s.bedrock_model_id} in {PRICES}")

    todo: list[tuple[Settings, dict, str]] = []
    est = 0.0
    for s in models:
        for it in items:
            digest = media_hash(it)
            if not cache_path(s, digest).exists():
                todo.append((s, it, digest))
                est += estimate(prices, s.bedrock_model_id, len(it["media"]))
    spent = ledger_total()
    by_source: dict[str, int] = defaultdict(int)
    for it in items:
        by_source[it["source"]] += 1
    print(f"items: {len(items)} " + ", ".join(f"{k}={v}" for k, v in sorted(by_source.items())))
    print(f"models: {', '.join(s.bedrock_model_id for s in models)}")
    print(f"uncached calls: {len(todo)}; estimated ${est:.2f}; ledger so far ${spent:.4f}; "
          f"cap ${args.max_usd:.2f}")  # fmt: skip
    if args.dry_run:
        return
    if spent + est > args.max_usd:
        sys.exit("estimate would exceed the cap; not calling any model")

    extractors = {s.bedrock_model_id: BedrockExtractor(make_bedrock_client(s), s) for s in models}
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    for i, (s, it, digest) in enumerate(todo, 1):
        pred = call(extractors[s.bedrock_model_id], it, prices)
        pred["called_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        path = cache_path(s, digest)
        if pred["ok"]:  # failures are not cached, so a re-run retries them
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(pred))
        # Failed calls report no usage, so they are charged at the estimate to keep the cap safe.
        usd = (
            pred["usd"]
            if pred["usd"] is not None
            else estimate(prices, s.bedrock_model_id, len(it["media"]))
        )
        entry = {
            "model": s.bedrock_model_id,
            "item": it["id"],
            "usd": round(usd, 6),
            "estimated": pred["usd"] is None,
            "at": pred["called_at"],
        }
        with LEDGER.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        status = "ok" if pred["ok"] else "FAILED"
        print(f"[{i}/{len(todo)}] {s.bedrock_model_id} {it['id']} {status}")

    per_item = []
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in models:
        for it in items:
            path = cache_path(s, media_hash(it))
            pred = json.loads(path.read_text()) if path.exists() else {
                "ok": False, "error": "no result (call failed)", "latency_ms": None, "usd": None,
            }  # fmt: skip
            if pred.get("latency_ms") is None:
                pred["latency_ms"] = 0.0
            groups[(it["source"], s.bedrock_model_id)].append({"item": it, "pred": pred})
            per_item.append({"id": it["id"], "model": s.bedrock_model_id, **pred})

    report = {
        "script": "eval/extraction.py",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "prompt_version": models[0].extraction_prompt_version,
        "models": [
            {"model_id": s.bedrock_model_id, "structured_output": s.bedrock_structured_output}
            for s in models
        ],
        "prices": prices,
        "items_files": sorted(str(f) for f in LABELS.glob("items_*.jsonl")),
        "selection_rules": "eval/SELECTION.md",
        "per_source": [
            {"source": src, "model": model, **summarize(rows)}
            for (src, model), rows in sorted(groups.items())
        ],
        "ledger_total_usd": round(ledger_total(), 4),
        "per_item": per_item,
        "caveats": [
            "Sources are reported separately and must not be pooled into one headline number.",
            "ring_playground has N=2 and its frames were seen in the 2026-10-15 smoke test.",
            "synthetic_overlay images are composites; they test counting and label reading, "
            "not realism.",
            "Carrier accuracy exists only where carrier truth exists (synthetic_overlay).",
            "Latency is wall clock per call, run sequentially from one client, including a retry.",
            "The two models run with different output modes (see models[].structured_output).",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print_table(report)
    print(f"\nwrote {args.out}")


def _pct(r: dict) -> str:
    if r["n"] == 0:
        return "n/a"
    lo, hi = r["ci95"]
    return f"{r['value'] * 100:.1f}% ({r['k']}/{r['n']}, CI {lo * 100:.0f}-{hi * 100:.0f}%)"


def print_table(report: dict) -> None:
    print("\n| Source | Model | N | Count exact | MAE | False alarms (neg) | Carrier | "
          "Fail | p50 ms | p95 ms | $/call |")  # fmt: skip
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in report["per_source"]:
        print(
            f"| {r['source']} | {r['model']} | {r['n']} | {_pct(r['count_exact'])} | "
            f"{r['count_mae']} | {_pct(r['negatives_false_alarm'])} | "
            f"{_pct(r['carrier_accuracy'])} | {r['failures']} | {r['latency_ms_p50']} | "
            f"{r['latency_ms_p95']} | {r['usd_per_call']} |"
        )


if __name__ == "__main__":
    main()
