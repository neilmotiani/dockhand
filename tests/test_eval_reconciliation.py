import json
from datetime import time

import pytest

from app.reconcile import ReconcileConfig, Status
from eval import reconciliation as er
from eval.simulate import CAFE_SUPPLIERS, Day, Rates, TruthRow, simulate, to_json


def test_simulate_is_deterministic() -> None:
    assert to_json(simulate(2, seed=1)) == to_json(simulate(2, seed=1))
    assert to_json(simulate(2, seed=1)) != to_json(simulate(2, seed=2))


def test_default_run_has_at_least_200_deliveries() -> None:
    days = simulate(er.DEFAULT_WEEKS, seed=er.DEFAULT_SEEDS[0])
    assert sum(len(d.deliveries) for d in days) >= er.MIN_DELIVERIES


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_truth_accounts_for_every_item(seed: int) -> None:
    for day in simulate(4, seed):
        e_ids = [t.expected_id for t in day.truth if t.expected_id]
        d_ids = [t.delivery_id for t in day.truth if t.delivery_id]
        assert sorted(e_ids) == sorted(e.id for e in day.expected)
        assert sorted(d_ids) == sorted(d.id for d in day.deliveries)
        for d in day.deliveries:
            assert d.detected_at.date() == day.day
            assert d.detected_at < day.reconcile_at


def test_truth_status_consistent_with_counts() -> None:
    for day in simulate(10, seed=4):
        exp = {e.id: e for e in day.expected}
        dets = {d.id: d for d in day.deliveries}
        for t in day.truth:
            if t.expected_id and t.delivery_id:
                n, got = exp[t.expected_id].expected_count, dets[t.delivery_id].package_count
                want = Status.MATCHED if got == n else Status.SHORT if got < n else Status.OVER
                assert t.status is want


def test_injection_rates_respected() -> None:
    none = Rates(0, 0, 0, 0, 0, 0, 0)
    days = simulate(4, seed=5, rates=none)
    assert all(t.status is Status.MATCHED and not t.tags for d in days for t in d.truth)

    always_missing = Rates(missing=1.0, extra_per_day=0)
    days = simulate(2, seed=5, rates=always_missing)
    assert all(t.status is Status.MISSING for d in days for t in d.truth)
    assert not any(d.deliveries for d in days)


def test_mislabel_changes_carrier() -> None:
    days = simulate(4, seed=6, rates=Rates(0, 0, 0, 0, 0, mislabel=1.0, extra_per_day=0))
    carrier_of = {s.name: s.carrier for s in CAFE_SUPPLIERS}
    for day in days:
        exp = {e.id: e for e in day.expected}
        dets = {d.id: d for d in day.deliveries}
        for t in day.truth:
            assert dets[t.delivery_id].carrier != carrier_of[exp[t.expected_id].supplier]


def test_late_and_early_are_outside_window() -> None:
    for day in simulate(20, seed=8):
        exp = {e.id: e for e in day.expected}
        dets = {d.id: d for d in day.deliveries}
        for t in day.truth:
            if not (t.expected_id and t.delivery_id):
                continue
            e, d = exp[t.expected_id], dets[t.delivery_id]
            if "late" in t.tags:
                assert d.detected_at > e.window_end
            elif "early" in t.tags:
                assert d.detected_at < e.window_start
            else:
                assert e.window_start <= d.detected_at <= e.window_end


def test_wilson() -> None:
    assert er.wilson(0, 0) is None
    lo, hi = er.wilson(5, 10)
    assert lo < 0.5 < hi
    lo, hi = er.wilson(10, 10)
    assert hi == pytest.approx(1.0) and lo < 1.0


def test_perfect_scores_on_clean_data() -> None:
    clean = Rates(0, 0, 0, 0, 0, 0, 0)
    days = simulate(3, seed=9, rates=clean)
    s = er.summarize(er.evaluate_days(days, ReconcileConfig()))
    assert s["pair_accuracy"]["value"] == 1.0
    assert s["per_status"]["matched"]["precision"]["value"] == 1.0
    assert s["discrepancy_micro"]["precision"]["n"] == 0
    assert s["discrepancy_micro"]["precision"]["value"] is None


def test_metrics_count_wrong_pair_as_error(monkeypatch) -> None:
    # Hand-built day where the matcher's answer differs from the truth on purpose: truth
    # says the delivery is unexpected and the order missing, but they pair up.
    days = simulate(1, seed=10, rates=Rates(0, 0, 0, 0, 0, 0, 0))
    day = next(d for d in days if d.expected)
    e, d = day.expected[0], day.deliveries[0]
    fake = Day(
        day.day,
        [e],
        [d],
        [TruthRow(e.id, None, Status.MISSING), TruthRow(None, d.id, Status.UNEXPECTED)],
    )
    s = er.summarize(er.evaluate_days([fake], ReconcileConfig()))
    assert s["per_status"]["missing"]["recall"] == {
        "value": 0.0,
        "k": 0,
        "n": 1,
        "ci95": er.wilson(0, 1),
    }
    assert s["per_status"]["unexpected"]["recall"]["value"] == 0.0
    assert s["per_status"]["matched"]["precision"]["value"] == 0.0
    assert s["confusion_expected_truth_vs_pred"] == {"missing": {"matched": 1}}


def test_merge_counts() -> None:
    a = {"n_days": 1, "tp_by_status": {"matched": 2}, "confusion": {"matched": {"matched": 2}}}
    b = {
        "n_days": 2,
        "tp_by_status": {"matched": 1, "short": 1},
        "confusion": {"matched": {"short": 1}},
    }
    assert er.merge_counts([a, b]) == {
        "n_days": 3,
        "tp_by_status": {"matched": 3, "short": 1},
        "confusion": {"matched": {"matched": 2, "short": 1}},
    }


def test_run_and_main_write_results(tmp_path, monkeypatch, capsys) -> None:
    out = tmp_path / "r.json"
    monkeypatch.setattr("sys.argv", ["x", "--seeds", "1", "2", "--weeks", "2", "--out", str(out)])
    er.main()
    res = json.loads(out.read_text())
    assert res["seeds"] == [1, 2]
    assert res["config"]["grace_minutes"] == 60
    assert set(res["per_seed"]) == {"1", "2"}
    pooled = res["pooled"]
    assert pooled["n_deliveries"] == sum(res["per_seed"][s]["n_deliveries"] for s in ("1", "2"))
    assert "discrepancy precision" in capsys.readouterr().out


def test_simulate_main(tmp_path, monkeypatch) -> None:
    out = tmp_path / "sim.json"
    monkeypatch.setattr("sys.argv", ["x", "--weeks", "1", "--seed", "3", "--out", str(out)])
    from eval import simulate as sim

    sim.main()
    data = json.loads(out.read_text())
    assert len(data) == 7
    assert {"day", "expected", "deliveries", "truth"} <= set(data[0])


def test_bakery_window_early_stays_same_day() -> None:
    bakery = next(s for s in CAFE_SUPPLIERS if s.name == "Daybreak Bakery")
    # 05:30 minus the maximum early offset (2 h) must not cross midnight.
    assert bakery.window[0] >= time(2, 0)
