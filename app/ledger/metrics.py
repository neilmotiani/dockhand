"""Headline numbers for the dashboard, read from the committed results files.

Nothing here computes a metric: every value is copied from a file that an eval or bench script
wrote (eval/results/*.json, bench/results/latency_*.json), together with its N, the file name
and the commit it was generated at, so the page can cite its source. A missing or unreadable
file gives `null` for that section rather than a made-up default.
"""

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SOURCE_LABELS = {
    "roboflow_porch_piracy": "Porch Piracy (Roboflow, CC BY 4.0)",
    "roboflow_packages_pd": "Packages (Roboflow, Public Domain)",
    "synthetic_overlay": "Synthetic composites on Ring Playground frames",
    "ring_playground": "Ring Playground live view",
}
# Real footage first, then synthetic, then the N=2 smoke test.
SOURCE_ORDER = list(SOURCE_LABELS)
MODEL_LABELS = {
    "us.amazon.nova-pro-v1:0": "Nova Pro",
    "us.anthropic.claude-sonnet-4-6": "Sonnet 4.6",
}
# Which capture rule produced each committed latency file (README, "Ingest pipeline").
LATENCY_LABELS = {
    "latency_playground_manual": "Fixed 25 s capture",
    "latency_playground_manual_settle": "Scene-settled capture",
}
LATENCY_STAGES = ("whep_setup", "capture", "extract", "reconcile")


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.exception("unreadable results file %s", path)
        return None


def _ratio(m: dict | None) -> dict | None:
    """{value, k, n} from an eval ratio, or None when it has no denominator."""
    if not m or not m.get("n"):
        return None
    return {"value": m["value"], "k": m["k"], "n": m["n"]}


def _stage(s: dict | None) -> dict | None:
    if not s:
        return None
    return {"n": s["n"], "p50_ms": s["p50_ms"], "p95_ms": s["p95_ms"], "max_ms": s["max_ms"]}


def extraction(root: Path) -> dict | None:
    rel = "eval/results/extraction.json"
    data = _read(root / rel)
    if data is None:
        return None
    rows = sorted(
        data["per_source"],
        key=lambda x: (
            SOURCE_ORDER.index(x["source"]) if x["source"] in SOURCE_ORDER else len(SOURCE_ORDER),
            x["model"],
        ),
    )
    return {
        "file": rel,
        "generated_at": data["generated_at"],
        "git_commit": data["git_commit"],
        "prompt_version": data["prompt_version"],
        "rows": [
            {
                "source": x["source"],
                "source_label": SOURCE_LABELS.get(x["source"], x["source"]),
                "model": x["model"],
                "model_label": MODEL_LABELS.get(x["model"], x["model"]),
                "n": x["n"],
                "count_exact": _ratio(x["count_exact"]),
                "carrier_accuracy": _ratio(x["carrier_accuracy"]),
                "false_alarm": _ratio(x["negatives_false_alarm"]),
                "latency_ms_p50": x["latency_ms_p50"],
                "usd_per_call": x["usd_per_call"],
            }
            for x in rows
        ],
        "caveats": data.get("caveats", []),
    }


def reconciliation(root: Path) -> dict | None:
    rel = "eval/results/reconciliation.json"
    data = _read(root / rel)
    if data is None:
        return None
    pooled = data["pooled"]
    return {
        "file": rel,
        "generated_at": data["generated_at"],
        "git_commit": data["git_commit"],
        "data": data["data"],
        "n_days": pooled["n_days"],
        "n_expected": pooled["n_expected"],
        "n_deliveries": pooled["n_deliveries"],
        "precision": _ratio(pooled["discrepancy_micro"]["precision"]),
        "recall": _ratio(pooled["discrepancy_micro"]["recall"]),
        "caveats": data.get("caveats", []),
    }


def latency(root: Path) -> list[dict]:
    out = []
    for path in sorted((root / "bench/results").glob("latency_*.json")):
        data = _read(path)
        if data is None:
            continue
        stages = data.get("stages", {})
        out.append(
            {
                "file": f"bench/results/{path.name}",
                "label": LATENCY_LABELS.get(path.stem, path.stem),
                "generated_at": data["generated_at"],
                "git_commit": data["git_commit"],
                "filters": data.get("filters", {}),
                "n_deliveries": data["n_deliveries"],
                "percentile_method": data.get("percentile_method"),
                "event_to_ledger": _stage(stages.get("event_to_ledger")),
                "stages": {s: _stage(stages.get(s)) for s in LATENCY_STAGES},
            }
        )
    out.sort(key=lambda x: x["generated_at"])
    return out


def load(root: Path) -> dict[str, Any]:
    return {
        "extraction": extraction(root),
        "reconciliation": reconciliation(root),
        "latency": latency(root),
    }
