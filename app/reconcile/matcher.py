"""Match detected deliveries to expected deliveries and assign statuses.

`reconcile()` is a pure function of (deliveries, expected, now, config). It always recomputes the
full answer from scratch instead of updating previous decisions, which is what makes it
idempotent: replaying a webhook, re-running the timer, or receiving inputs in a different order
yields the same rows with the same IDs. The persistence layer replaces the stored set for the
same inputs in one transaction.
"""

import uuid
from datetime import datetime

import numpy as np
from scipy.optimize import linear_sum_assignment

from app.reconcile.cost import (
    ReconcileConfig,
    build_cost_matrix,
    carriers_agree,
    hours_outside_window,
)
from app.reconcile.models import (
    DetectedDelivery,
    ExpectedDelivery,
    ReconcileResult,
    Reconciliation,
    Status,
)


def solve_assignment(cost: np.ndarray, max_cost: float) -> list[tuple[int, int]]:
    """Return (row, col) pairs with cost < max_cost that minimize total cost, where leaving a
    row or column unpaired is also allowed.

    Running linear_sum_assignment on the raw matrix and thresholding afterwards can throw away
    a good pair: the solver must pair min(rows, cols) items, so it may trade a perfect pair for
    two mediocre ones that then both fail the threshold. Instead we add one "unpaired" slot per
    row and per column, each costing max_cost / 2. Pairing i with j then beats leaving both
    unpaired exactly when cost[i, j] < max_cost, and the solver weighs that globally.

    Layout for n rows and m columns (B is a prohibitive cost):

                    m real cols      n row-dummies
        n rows    [ cost            diag(max/2), B elsewhere ]
        m col-    [ diag(max/2),    0                        ]
          dummies   B elsewhere
    """
    n, m = cost.shape
    if n == 0 or m == 0:
        return []
    half = max_cost / 2
    # Leaving everything unpaired costs (n + m) * half, so any single B entry is worse than that.
    big = (n + m) * max_cost + 1.0
    real = np.where(cost < max_cost, cost, big)

    size = n + m
    aug = np.full((size, size), big)
    aug[:n, :m] = real
    aug[:n, m:][np.arange(n), np.arange(n)] = half
    aug[n:, :m][np.arange(m), np.arange(m)] = half
    aug[n:, m:] = 0.0

    rows, cols = linear_sum_assignment(aug)
    return [(int(r), int(c)) for r, c in zip(rows, cols, strict=True) if r < n and c < m]


def _fmt_time(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M")


def _fmt_offset(hours: float) -> str:
    minutes = round(abs(hours) * 60)
    span = f"{minutes} min" if minutes < 120 else f"{abs(hours):.1f} h"
    return f"{span} {'early' if hours < 0 else 'late'}"


def _boxes(n: int) -> str:
    return f"{n} box" if n == 1 else f"{n} boxes"


def _pair_reason(status: Status, d: DetectedDelivery, e: ExpectedDelivery) -> str:
    if status is Status.MATCHED:
        head = f"Matched: {_boxes(d.package_count)} as expected"
    elif status is Status.SHORT:
        missing = e.expected_count - d.package_count
        head = f"Short by {missing}: {d.package_count} of {_boxes(e.expected_count)} delivered"
    else:
        extra = d.package_count - e.expected_count
        head = f"Over by {extra}: {d.package_count} boxes delivered, {e.expected_count} expected"
    notes = [f"{e.supplier} at {_fmt_time(d.detected_at)}"]
    off = hours_outside_window(d, e)
    if off:
        notes.append(_fmt_offset(off))
    agree = carriers_agree(d.carrier, e.carrier)
    if agree is False:
        notes.append(f"carrier read as {d.carrier}, expected {e.carrier}")
    elif agree is None and e.carrier:
        notes.append(f"carrier unreadable ({d.carrier or 'blank'}), expected {e.carrier}")
    return f"{head} ({'; '.join(notes)})."


def _missing_reason(e: ExpectedDelivery, cfg: ReconcileConfig) -> str:
    grace_min = round(cfg.grace.total_seconds() / 60)
    ref = f" {e.reference}" if e.reference else ""
    return (
        f"Missing: no delivery matched {e.supplier}{ref} ({_boxes(e.expected_count)}, window "
        f"{_fmt_time(e.window_start)} to {_fmt_time(e.window_end)}, +{grace_min} min grace)."
    )


def _unexpected_reason(
    d: DetectedDelivery,
    row: np.ndarray,
    expected: list[ExpectedDelivery],
    taken: set[int],
    cfg: ReconcileConfig,
) -> str:
    base = (
        f"Unexpected: {d.carrier} delivery of {_boxes(d.package_count)} at "
        f"{_fmt_time(d.detected_at)} matched no expected delivery"
    )
    # Explain the nearest miss so a human can override the decision if it was wrong.
    feasible = [j for j in range(len(expected)) if np.isfinite(row[j])]
    if not feasible:
        return base + "."
    j = min(feasible, key=lambda k: (row[k], k))
    e = expected[j]
    if j in taken:
        why = "it was assigned to a better-fitting delivery"
    else:
        why = f"cost {row[j]:.2f} is not below the {cfg.max_cost:.2f} threshold"
    return base + f"; closest was {e.supplier}, but {why}."


def reconcile(
    deliveries: list[DetectedDelivery],
    expected: list[ExpectedDelivery],
    now: datetime,
    cfg: ReconcileConfig | None = None,
) -> ReconcileResult:
    """Reconcile one batch (typically one business day) of deliveries against expectations.

    - Each delivery ends up in exactly one row: paired (matched/short/over) or unexpected.
    - Each expected delivery ends up in exactly one row (paired or missing), or in
      `pending_expected_ids` if it is unpaired and `now` is before `window_end + grace`.
    """
    cfg = cfg or ReconcileConfig()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    # Dedupe by ID (a replayed event can surface the same delivery twice) and sort so the cost
    # matrix, and therefore the solver's tie-breaking, does not depend on input order.
    ds = sorted({d.id: d for d in deliveries}.values(), key=lambda d: (d.detected_at, d.id))
    es = sorted({e.id: e for e in expected}.values(), key=lambda e: (e.window_start, e.id))

    cost = build_cost_matrix(ds, es, cfg)
    pairs = solve_assignment(cost, cfg.max_cost)
    paired_d = {i for i, _ in pairs}
    paired_e = {j for _, j in pairs}

    out: list[Reconciliation] = []
    for i, j in pairs:
        d, e = ds[i], es[j]
        if d.package_count == e.expected_count:
            status = Status.MATCHED
        elif d.package_count < e.expected_count:
            status = Status.SHORT
        else:
            status = Status.OVER
        out.append(
            Reconciliation(
                id=Reconciliation.make_id(e.id, d.id),
                expected_id=e.id,
                delivery_id=d.id,
                status=status,
                score=round(max(0.0, 1.0 - float(cost[i, j]) / cfg.max_cost), 4),
                reason=_pair_reason(status, d, e),
                decided_at=now,
            )
        )

    pending: list[uuid.UUID] = []
    for j, e in enumerate(es):
        if j in paired_e:
            continue
        if now < e.window_end + cfg.grace:
            pending.append(e.id)
            continue
        out.append(
            Reconciliation(
                id=Reconciliation.make_id(e.id, None),
                expected_id=e.id,
                delivery_id=None,
                status=Status.MISSING,
                score=None,
                reason=_missing_reason(e, cfg),
                decided_at=now,
            )
        )

    for i, d in enumerate(ds):
        if i in paired_d:
            continue
        out.append(
            Reconciliation(
                id=Reconciliation.make_id(None, d.id),
                expected_id=None,
                delivery_id=d.id,
                status=Status.UNEXPECTED,
                score=None,
                reason=_unexpected_reason(d, cost[i], es, paired_e, cfg),
                decided_at=now,
            )
        )

    out.sort(key=lambda r: r.id)
    return ReconcileResult(reconciliations=out, pending_expected_ids=pending)


def next_missing_deadline(
    expected: list[ExpectedDelivery], result: ReconcileResult, cfg: ReconcileConfig | None = None
) -> datetime | None:
    """When the timer should next re-run reconciliation: the earliest deadline among pending
    expected deliveries, or None if nothing is pending."""
    cfg = cfg or ReconcileConfig()
    pending = set(result.pending_expected_ids)
    deadlines = [e.window_end + cfg.grace for e in expected if e.id in pending]
    return min(deadlines, default=None)
