import pytest

from bench.latency import nearest_rank, summarize


def test_nearest_rank_returns_observed_values():
    v = [5.0, 1.0, 3.0, 2.0, 4.0]
    assert nearest_rank(v, 50) == 3.0
    assert nearest_rank(v, 95) == 5.0
    assert nearest_rank(v, 0) == 1.0
    assert nearest_rank(list(range(1, 101)), 95) == 95
    with pytest.raises(ValueError):
        nearest_rank([], 50)


def test_summarize_reports_n_per_stage_and_skips_missing():
    rows = [
        {"event_to_ledger": 9000.0, "extract": 900.0, "ring_to_received": 1000.0},
        {"event_to_ledger": 11000.0, "extract": 1100.0, "ring_to_received": None},
        {"event_to_ledger": 10000.0, "extract": 1000.0, "custom": 1, "flag": True},
    ]
    s = summarize(rows)
    assert list(s)[:3] == ["event_to_ledger", "ring_to_received", "extract"]
    assert s["event_to_ledger"] == {
        "n": 3,
        "p50_ms": 10000.0,
        "p95_ms": 11000.0,
        "max_ms": 11000.0,
    }
    assert s["ring_to_received"]["n"] == 1
    assert "flag" not in s and s["custom"]["n"] == 1


async def test_load_filters_by_detected_at(engine, database_url):
    from datetime import timedelta

    from bench.latency import load
    from tests.pipeline_fakes import T0
    from tests.test_reconcile_store import add_delivery

    for minutes in (0, 30, 60):
        await add_delivery(engine, T0 + timedelta(minutes=minutes))
    rows = await load(database_url, None, None, T0 + timedelta(minutes=15), T0 + timedelta(hours=1))
    assert len(rows) == 1
    assert len(await load(database_url, None, None)) == 3
