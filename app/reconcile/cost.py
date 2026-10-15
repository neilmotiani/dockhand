"""Pairwise cost between a detected delivery and an expected delivery.

cost = carrier term + time term + count term, each in the same units as `max_cost` (the
acceptance threshold). A pair is accepted only if its cost is below `max_cost`, so each weight
can be read as "how much of the budget this kind of disagreement uses up":

- carrier: 0 if the carriers agree, `carrier_unknown_cost` if either side is unknown, else
  `carrier_mismatch_cost`. The mismatch cost is deliberately below `max_cost`: the vision model
  can misread a carrier, and a box arriving on time with the right count should still match.
- time: 0 inside the window; `time_cost_per_hour` per hour outside it (early or late). Beyond
  `max_offset_hours` the pair is infeasible (cost = inf) no matter what else agrees.
- count: `count_cost_weight` * |delivered - expected| / expected, capped at one weight.
  Relative, so being 1 short of 2 costs more than being 1 short of 10.
"""

import math
from dataclasses import dataclass
from datetime import timedelta

import numpy as np

from app.config import Settings
from app.reconcile.models import UNKNOWN_CARRIERS, DetectedDelivery, ExpectedDelivery


@dataclass(frozen=True)
class ReconcileConfig:
    grace: timedelta = timedelta(minutes=60)
    max_cost: float = 1.0
    carrier_mismatch_cost: float = 0.5
    carrier_unknown_cost: float = 0.2
    time_cost_per_hour: float = 0.15
    max_offset_hours: float = 8.0
    count_cost_weight: float = 0.5

    @classmethod
    def from_settings(cls, s: Settings) -> "ReconcileConfig":
        return cls(
            grace=timedelta(minutes=s.reconcile_grace_minutes),
            max_cost=s.reconcile_max_cost,
            carrier_mismatch_cost=s.reconcile_carrier_mismatch_cost,
            carrier_unknown_cost=s.reconcile_carrier_unknown_cost,
            time_cost_per_hour=s.reconcile_time_cost_per_hour,
            max_offset_hours=s.reconcile_max_offset_hours,
            count_cost_weight=s.reconcile_count_cost_weight,
        )


def normalize_carrier(carrier: str | None) -> str:
    return (carrier or "").strip().casefold()


def carriers_agree(a: str | None, b: str | None) -> bool | None:
    """True/False if both carriers are known, None if either is unknown."""
    na, nb = normalize_carrier(a), normalize_carrier(b)
    if na in UNKNOWN_CARRIERS or nb in UNKNOWN_CARRIERS:
        return None
    return na == nb


def hours_outside_window(d: DetectedDelivery, e: ExpectedDelivery) -> float:
    """Signed hours outside the window: negative = early, positive = late, 0 = inside."""
    if d.detected_at < e.window_start:
        return (d.detected_at - e.window_start).total_seconds() / 3600
    if d.detected_at > e.window_end:
        return (d.detected_at - e.window_end).total_seconds() / 3600
    return 0.0


def carrier_cost(d: DetectedDelivery, e: ExpectedDelivery, cfg: ReconcileConfig) -> float:
    agree = carriers_agree(d.carrier, e.carrier)
    if agree is None:
        return cfg.carrier_unknown_cost
    return 0.0 if agree else cfg.carrier_mismatch_cost


def time_cost(d: DetectedDelivery, e: ExpectedDelivery, cfg: ReconcileConfig) -> float:
    off = abs(hours_outside_window(d, e))
    if off > cfg.max_offset_hours:
        return math.inf
    return off * cfg.time_cost_per_hour


def count_cost(d: DetectedDelivery, e: ExpectedDelivery, cfg: ReconcileConfig) -> float:
    rel = abs(d.package_count - e.expected_count) / e.expected_count
    return cfg.count_cost_weight * min(rel, 1.0)


def pair_cost(d: DetectedDelivery, e: ExpectedDelivery, cfg: ReconcileConfig) -> float:
    return carrier_cost(d, e, cfg) + time_cost(d, e, cfg) + count_cost(d, e, cfg)


def build_cost_matrix(
    deliveries: list[DetectedDelivery], expected: list[ExpectedDelivery], cfg: ReconcileConfig
) -> np.ndarray:
    """Rows are deliveries, columns are expected deliveries. Infeasible pairs are inf."""
    m = np.empty((len(deliveries), len(expected)), dtype=float)
    for i, d in enumerate(deliveries):
        for j, e in enumerate(expected):
            m[i, j] = pair_cost(d, e, cfg)
    return m
