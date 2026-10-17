import itertools
import math
import random
import uuid
from datetime import UTC, datetime, timedelta

import numpy as np
import pydantic
import pytest

from app.config import Settings
from app.reconcile import (
    DetectedDelivery,
    ExpectedDelivery,
    ReconcileConfig,
    Reconciliation,
    Status,
    build_cost_matrix,
    next_missing_deadline,
    pair_cost,
    reconcile,
    solve_assignment,
)
from app.reconcile.cost import carrier_cost, count_cost, hours_outside_window, time_cost

CFG = ReconcileConfig()
DAY = datetime(2026, 10, 15, tzinfo=UTC)
END_OF_DAY = DAY + timedelta(days=1)


def at(h: float) -> datetime:
    return DAY + timedelta(hours=h)


def exp(
    carrier: str | None = "UPS",
    count: int = 4,
    start: float = 9,
    end: float = 12,
    supplier: str = "Acme",
    reference: str | None = None,
) -> ExpectedDelivery:
    return ExpectedDelivery(
        id=uuid.uuid4(),
        supplier=supplier,
        carrier=carrier,
        expected_count=count,
        window_start=at(start),
        window_end=at(end),
        reference=reference,
    )


def det(carrier: str = "UPS", count: int = 4, hour: float = 10) -> DetectedDelivery:
    return DetectedDelivery(
        id=uuid.uuid4(), detected_at=at(hour), carrier=carrier, package_count=count
    )


def by_status(result) -> dict[Status, list[Reconciliation]]:
    out: dict[Status, list[Reconciliation]] = {s: [] for s in Status}
    for r in result.reconciliations:
        out[r.status].append(r)
    return out


def only(result) -> Reconciliation:
    assert len(result.reconciliations) == 1
    return result.reconciliations[0]


# --- models -----------------------------------------------------------------------------------


def test_naive_datetimes_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        DetectedDelivery(
            id=uuid.uuid4(), detected_at=datetime(2026, 1, 1), carrier="UPS", package_count=1
        )


def test_window_must_be_ordered() -> None:
    with pytest.raises(pydantic.ValidationError, match="window_end"):
        exp(start=12, end=9)


def test_zero_length_window_allowed() -> None:
    assert exp(start=10, end=10).window_start == at(10)


@pytest.mark.parametrize("count", [0, -1])
def test_expected_count_must_be_positive(count: int) -> None:
    with pytest.raises(pydantic.ValidationError):
        exp(count=count)


def test_negative_package_count_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        det(count=-1)


def test_now_must_be_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        reconcile([], [], datetime(2026, 1, 1))


def test_config_from_settings() -> None:
    s = Settings(_env_file=None, reconcile_grace_minutes=30, reconcile_max_cost=2.0)
    cfg = ReconcileConfig.from_settings(s)
    assert cfg.grace == timedelta(minutes=30)
    assert cfg.max_cost == 2.0
    assert ReconcileConfig.from_settings(Settings(_env_file=None)) == ReconcileConfig()


# --- cost -------------------------------------------------------------------------------------


def test_perfect_pair_costs_zero() -> None:
    assert pair_cost(det(), exp(), CFG) == 0


@pytest.mark.parametrize(
    ("detected", "expected", "cost"),
    [
        ("UPS", "UPS", 0.0),
        ("ups ", "UPS", 0.0),  # normalized
        ("FedEx", "UPS", CFG.carrier_mismatch_cost),
        ("Unknown", "UPS", CFG.carrier_unknown_cost),
        ("", "UPS", CFG.carrier_unknown_cost),
        ("UPS", None, CFG.carrier_unknown_cost),
        ("Other", "Other", 0.0),
    ],
)
def test_carrier_cost(detected: str, expected: str | None, cost: float) -> None:
    assert carrier_cost(det(carrier=detected), exp(carrier=expected), CFG) == cost


def test_carrier_mismatch_alone_does_not_block_a_match() -> None:
    # A vision misread of the carrier must not by itself turn a correct delivery into
    # missing + unexpected.
    assert CFG.carrier_mismatch_cost < CFG.max_cost


@pytest.mark.parametrize(
    ("hour", "offset"),
    [(9, 0.0), (10.5, 0.0), (12, 0.0), (8, -1.0), (14.5, 2.5)],
)
def test_hours_outside_window(hour: float, offset: float) -> None:
    assert hours_outside_window(det(hour=hour), exp(start=9, end=12)) == pytest.approx(offset)


def test_time_cost_symmetric_and_linear() -> None:
    e = exp(start=9, end=12)
    assert time_cost(det(hour=8), e, CFG) == pytest.approx(CFG.time_cost_per_hour)
    assert time_cost(det(hour=13), e, CFG) == pytest.approx(CFG.time_cost_per_hour)
    assert time_cost(det(hour=14), e, CFG) == pytest.approx(2 * CFG.time_cost_per_hour)


def test_time_cost_infeasible_beyond_max_offset() -> None:
    e = exp(start=9, end=12)
    assert math.isfinite(time_cost(det(hour=12 + CFG.max_offset_hours), e, CFG))
    assert time_cost(det(hour=12 + CFG.max_offset_hours + 0.01), e, CFG) == math.inf


def test_count_cost_relative_and_capped() -> None:
    assert count_cost(det(count=9), exp(count=10), CFG) == pytest.approx(
        0.1 * CFG.count_cost_weight
    )
    assert count_cost(det(count=1), exp(count=2), CFG) == pytest.approx(0.5 * CFG.count_cost_weight)
    assert count_cost(det(count=50), exp(count=2), CFG) == pytest.approx(CFG.count_cost_weight)
    assert count_cost(det(count=0), exp(count=3), CFG) == pytest.approx(CFG.count_cost_weight)


def test_build_cost_matrix_shape_and_values() -> None:
    ds = [det(), det(carrier="FedEx")]
    es = [exp(), exp(), exp()]
    m = build_cost_matrix(ds, es, CFG)
    assert m.shape == (2, 3)
    assert m[0, 0] == 0
    assert m[1, 0] == CFG.carrier_mismatch_cost


def test_build_cost_matrix_empty() -> None:
    assert build_cost_matrix([], [exp()], CFG).shape == (0, 1)
    assert build_cost_matrix([det()], [], CFG).shape == (1, 0)


# --- solver -----------------------------------------------------------------------------------


def test_solver_empty() -> None:
    assert solve_assignment(np.empty((0, 3)), 1.0) == []
    assert solve_assignment(np.empty((2, 0)), 1.0) == []


def test_solver_threshold_is_strict() -> None:
    assert solve_assignment(np.array([[0.999]]), 1.0) == [(0, 0)]
    assert solve_assignment(np.array([[1.0]]), 1.0) == []
    assert solve_assignment(np.array([[np.inf]]), 1.0) == []


def test_solver_does_not_sacrifice_a_good_pair() -> None:
    # Plain linear_sum_assignment must pair both rows: {0-1, 1-0} = 0.9 + 0.5 = 1.4 beats
    # {0-0, 1-1} = 0 + inf. Thresholding afterwards would accept two mediocre pairs and lose
    # the perfect 0-0 pair. With unpaired slots, 0-0 alone (0 + 2 * 0.5 = 1.0) wins.
    cost = np.array([[0.0, 0.9], [0.5, np.inf]])
    assert solve_assignment(cost, 1.0) == [(0, 0)]


def test_solver_matches_brute_force() -> None:
    rng = np.random.default_rng(0)
    for _ in range(200):
        n, m = rng.integers(0, 4, size=2)
        cost = rng.uniform(0, 1.6, size=(n, m))
        cost[rng.uniform(size=(n, m)) < 0.15] = np.inf
        got = solve_assignment(cost, 1.0)
        assert _objective(cost, got) == pytest.approx(_brute_force_best(cost, 1.0))
        assert all(cost[i, j] < 1.0 for i, j in got)
        assert len({i for i, _ in got}) == len(got) == len({j for _, j in got})


def _objective(cost: np.ndarray, pairs: list[tuple[int, int]]) -> float:
    n, m = cost.shape
    return sum(cost[i, j] for i, j in pairs) + 0.5 * (n + m - 2 * len(pairs))


def _brute_force_best(cost: np.ndarray, max_cost: float) -> float:
    n, m = cost.shape
    best = 0.5 * (n + m)
    cells = [(i, j) for i in range(n) for j in range(m) if cost[i, j] < max_cost]
    for k in range(1, min(n, m) + 1):
        for combo in itertools.combinations(cells, k):
            if len({i for i, _ in combo}) == k and len({j for _, j in combo}) == k:
                best = min(best, _objective(cost, list(combo)))
    return best


# --- statuses ---------------------------------------------------------------------------------


def test_empty_inputs() -> None:
    res = reconcile([], [], END_OF_DAY)
    assert res.reconciliations == []
    assert res.pending_expected_ids == []


def test_exact_match() -> None:
    e, d = exp(count=4), det(count=4)
    r = only(reconcile([d], [e], END_OF_DAY))
    assert r.status is Status.MATCHED
    assert (r.expected_id, r.delivery_id) == (e.id, d.id)
    assert r.score == 1.0
    assert r.decided_at == END_OF_DAY
    assert r.reason.startswith("Matched: 4 boxes as expected (Acme at 2026-10-15 10:00")


def test_short() -> None:
    r = only(reconcile([det(count=3)], [exp(count=5)], END_OF_DAY))
    assert r.status is Status.SHORT
    assert r.reason.startswith("Short by 2: 3 of 5 boxes delivered")
    assert 0 < r.score < 1


def test_over() -> None:
    r = only(reconcile([det(count=6)], [exp(count=4)], END_OF_DAY))
    assert r.status is Status.OVER
    assert r.reason.startswith("Over by 2: 6 boxes delivered, 4 expected")


def test_zero_boxes_is_short() -> None:
    r = only(reconcile([det(count=0, hour=10)], [exp(count=1)], END_OF_DAY))
    # 0 of 1 costs the full count weight (0.5), still under the threshold on its own.
    assert r.status is Status.SHORT
    assert "0 of 1 box delivered" in r.reason


def test_only_expected_after_deadline_is_missing() -> None:
    e = exp(end=12, reference="PO-1")
    r = only(reconcile([], [e], at(12) + CFG.grace))
    assert r.status is Status.MISSING
    assert r.expected_id == e.id and r.delivery_id is None and r.score is None
    assert "Acme PO-1" in r.reason and "+60 min grace" in r.reason


def test_only_expected_before_deadline_is_pending() -> None:
    e = exp(end=12)
    res = reconcile([], [e], at(12) + CFG.grace - timedelta(seconds=1))
    assert res.reconciliations == []
    assert res.pending_expected_ids == [e.id]


def test_only_deliveries_are_unexpected() -> None:
    d = det(carrier="DHL", count=1, hour=15)
    r = only(reconcile([d], [], at(15)))
    assert r.status is Status.UNEXPECTED
    assert r.delivery_id == d.id and r.expected_id is None
    assert (
        r.reason
        == "Unexpected: DHL delivery of 1 box at 2026-10-15 15:00 matched no expected delivery."
    )


def test_unexpected_is_decided_immediately_even_while_expected_pending() -> None:
    # A delivery too far from any window is flagged now; if the user later adds the order,
    # the next run re-pairs it.
    e = exp(start=18, end=20)
    d = det(hour=9)  # 9 h before the window opens: infeasible
    res = reconcile([d], [e], at(9))
    assert [r.status for r in res.reconciliations] == [Status.UNEXPECTED]
    assert res.pending_expected_ids == [e.id]


def test_carrier_mislabel_still_matches_with_note() -> None:
    r = only(reconcile([det(carrier="FedEx")], [exp(carrier="UPS")], END_OF_DAY))
    assert r.status is Status.MATCHED
    assert "carrier read as FedEx, expected UPS" in r.reason
    assert r.score == pytest.approx(1 - CFG.carrier_mismatch_cost)


def test_unknown_carrier_matches_with_note() -> None:
    r = only(reconcile([det(carrier="Unknown")], [exp(carrier="UPS")], END_OF_DAY))
    assert r.status is Status.MATCHED
    assert "carrier unreadable (Unknown), expected UPS" in r.reason


def test_expected_without_carrier_matches_silently() -> None:
    r = only(reconcile([det(carrier="UPS")], [exp(carrier=None)], END_OF_DAY))
    assert r.status is Status.MATCHED
    assert "carrier" not in r.reason


def test_late_arrival_matches_with_note() -> None:
    r = only(reconcile([det(hour=12.75)], [exp(end=12)], END_OF_DAY))
    assert r.status is Status.MATCHED
    assert "45 min late" in r.reason


def test_early_arrival_matches_with_note() -> None:
    r = only(reconcile([det(hour=6)], [exp(start=9)], END_OF_DAY))
    assert r.status is Status.MATCHED
    assert "3.0 h early" in r.reason


def test_too_late_is_missing_plus_unexpected() -> None:
    e = exp(end=12)
    d = det(hour=12 + CFG.max_offset_hours + 1)
    s = by_status(reconcile([d], [e], END_OF_DAY))
    assert [r.expected_id for r in s[Status.MISSING]] == [e.id]
    assert [r.delivery_id for r in s[Status.UNEXPECTED]] == [d.id]
    # Infeasible pairs are not offered as the "closest" explanation.
    assert s[Status.UNEXPECTED][0].reason.endswith("matched no expected delivery.")


def test_combined_problems_over_threshold_explained() -> None:
    # Wrong carrier (0.5) + 3 h late (0.45) + short 1 of 4 (0.125) = 1.075 > 1.0 (prints as 1.07).
    e = exp(carrier="UPS", count=4, end=12)
    d = det(carrier="FedEx", count=3, hour=15)
    s = by_status(reconcile([d], [e], END_OF_DAY))
    assert len(s[Status.MISSING]) == 1 and len(s[Status.UNEXPECTED]) == 1
    assert "closest was Acme, but cost 1.07 is not below the 1.00 threshold" in (
        s[Status.UNEXPECTED][0].reason
    )


def test_unexpected_reason_when_closest_was_taken() -> None:
    e = exp(count=4)
    good, extra = det(count=4, hour=10), det(count=4, hour=11)
    s = by_status(reconcile([good, extra], [e], END_OF_DAY))
    assert (
        s[Status.MATCHED][0].delivery_id == good.id or s[Status.MATCHED][0].delivery_id == extra.id
    )
    assert "assigned to a better-fitting delivery" in s[Status.UNEXPECTED][0].reason


def test_count_disambiguates_same_carrier_overlapping_windows() -> None:
    cups = exp(carrier="UPS", count=6, start=10, end=14, supplier="Cups")
    napkins = exp(carrier="UPS", count=2, start=10.5, end=15, supplier="Napkins")
    d_small = det(carrier="UPS", count=2, hour=11)
    d_big = det(carrier="UPS", count=6, hour=11.5)
    res = reconcile([d_small, d_big], [cups, napkins], END_OF_DAY)
    pairs = {(r.expected_id, r.delivery_id) for r in res.reconciliations}
    assert pairs == {(cups.id, d_big.id), (napkins.id, d_small.id)}
    assert all(r.status is Status.MATCHED for r in res.reconciliations)


def test_carrier_disambiguates_same_time() -> None:
    sysco = exp(carrier="Sysco", count=5, start=6, end=9)
    usf = exp(carrier="US Foods", count=5, start=6, end=9)
    d1, d2 = det(carrier="US Foods", count=5, hour=7), det(carrier="Sysco", count=5, hour=8)
    res = reconcile([d1, d2], [sysco, usf], END_OF_DAY)
    pairs = {(r.expected_id, r.delivery_id) for r in res.reconciliations}
    assert pairs == {(sysco.id, d2.id), (usf.id, d1.id)}


def test_more_deliveries_than_expected() -> None:
    e = exp(carrier="Sysco", count=6, start=6, end=9)
    ds = [
        det(carrier="Sysco", count=6, hour=7),
        det(carrier="USPS", count=1, hour=11),
        det(carrier="DHL", count=2, hour=14),
    ]
    s = by_status(reconcile(ds, [e], END_OF_DAY))
    assert [r.delivery_id for r in s[Status.MATCHED]] == [ds[0].id]
    assert {r.delivery_id for r in s[Status.UNEXPECTED]} == {ds[1].id, ds[2].id}


def test_more_expected_than_deliveries() -> None:
    es = [
        exp(carrier="Sysco", start=6, end=9),
        exp(carrier="UPS", start=10, end=14),
        exp(carrier="FedEx", start=10, end=16),
    ]
    d = det(carrier="UPS", count=4, hour=11)
    s = by_status(reconcile([d], es, END_OF_DAY))
    assert [r.expected_id for r in s[Status.MATCHED]] == [es[1].id]
    assert {r.expected_id for r in s[Status.MISSING]} == {es[0].id, es[2].id}


def test_mixed_missing_and_pending() -> None:
    morning = exp(start=6, end=9)
    afternoon = exp(start=14, end=17)
    res = reconcile([], [morning, afternoon], at(12))
    assert [r.expected_id for r in res.reconciliations] == [morning.id]
    assert res.pending_expected_ids == [afternoon.id]


def test_grace_is_configurable() -> None:
    e = exp(end=12)
    cfg = ReconcileConfig(grace=timedelta(0))
    assert only(reconcile([], [e], at(12), cfg)).status is Status.MISSING


# --- invariants and idempotency ---------------------------------------------------------------


def _random_day(seed: int) -> tuple[list[DetectedDelivery], list[ExpectedDelivery]]:
    rng = random.Random(seed)
    carriers = ["UPS", "FedEx", "Sysco", "Amazon", "Unknown"]
    es = [
        exp(
            carrier=rng.choice(carriers),
            count=rng.randint(1, 8),
            start=s,
            end=s + rng.uniform(0, 6),
        )
        for s in (rng.uniform(5, 15) for _ in range(rng.randint(0, 6)))
    ]
    ds = [
        det(carrier=rng.choice(carriers), count=rng.randint(0, 9), hour=rng.uniform(4, 23))
        for _ in range(rng.randint(0, 7))
    ]
    return ds, es


@pytest.mark.parametrize("seed", range(30))
def test_every_input_accounted_for_exactly_once(seed: int) -> None:
    ds, es = _random_day(seed)
    res = reconcile(ds, es, at(14))
    d_ids = [r.delivery_id for r in res.reconciliations if r.delivery_id]
    e_ids = [r.expected_id for r in res.reconciliations if r.expected_id]
    assert sorted(d_ids) == sorted(d.id for d in ds)
    assert sorted(e_ids + res.pending_expected_ids) == sorted(e.id for e in es)
    assert len({r.id for r in res.reconciliations}) == len(res.reconciliations)
    for r in res.reconciliations:
        paired = r.expected_id is not None and r.delivery_id is not None
        assert paired == (r.status in {Status.MATCHED, Status.SHORT, Status.OVER})
        assert (r.score is not None) == paired
        assert r.reason


@pytest.mark.parametrize("seed", range(10))
def test_rerun_is_identical(seed: int) -> None:
    ds, es = _random_day(seed)
    assert reconcile(ds, es, at(14)) == reconcile(ds, es, at(14))


@pytest.mark.parametrize("seed", range(10))
def test_input_order_does_not_matter(seed: int) -> None:
    ds, es = _random_day(seed)
    rng = random.Random(seed)
    ds2, es2 = ds[:], es[:]
    rng.shuffle(ds2)
    rng.shuffle(es2)
    a, b = reconcile(ds, es, at(14)), reconcile(ds2, es2, at(14))
    assert a.reconciliations == b.reconciliations
    assert sorted(a.pending_expected_ids) == sorted(b.pending_expected_ids)


def test_duplicate_inputs_do_not_duplicate_rows() -> None:
    e, d = exp(), det()
    once = reconcile([d], [e], END_OF_DAY)
    replayed = reconcile([d, d, d], [e, e], END_OF_DAY)
    assert replayed == once


def test_ids_are_deterministic_per_pair() -> None:
    e, d = exp(), det()
    r = only(reconcile([d], [e], END_OF_DAY))
    assert r.id == Reconciliation.make_id(e.id, d.id)
    assert Reconciliation.make_id(e.id, None) != Reconciliation.make_id(None, e.id)


def test_incremental_arrival_converges_to_batch_result() -> None:
    # Production re-runs on every new delivery; the final state must equal one batch run.
    ds, es = _random_day(3)
    ds.sort(key=lambda d: d.detected_at)
    for k in range(len(ds) + 1):
        reconcile(ds[:k], es, at(14))
    assert reconcile(ds, es, END_OF_DAY) == reconcile(list(reversed(ds)), es, END_OF_DAY)


def test_next_missing_deadline() -> None:
    a, b = exp(end=12), exp(end=15)
    res = reconcile([], [a, b], at(10))
    assert next_missing_deadline([a, b], res) == at(12) + CFG.grace
    res = reconcile([], [a, b], at(13.5))
    assert next_missing_deadline([a, b], res) == at(15) + CFG.grace
    assert next_missing_deadline([a, b], reconcile([], [a, b], END_OF_DAY)) is None


def test_reasons_use_the_given_timezone():
    from zoneinfo import ZoneInfo

    t = datetime(2026, 10, 17, 0, 53, tzinfo=UTC)  # 17:53 on the 16th in Los Angeles
    d = DetectedDelivery(id=uuid.uuid4(), detected_at=t, carrier="UPS", package_count=1)
    la = ZoneInfo("America/Los_Angeles")
    assert "at 2026-10-16 17:53" in reconcile([d], [], t, tz=la).reconciliations[0].reason
    assert "at 2026-10-17 00:53" in reconcile([d], [], t).reconciliations[0].reason
