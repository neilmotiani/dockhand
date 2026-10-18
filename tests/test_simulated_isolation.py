"""Simulated demo history must never feed a reported number.

Every number in the README and on the "How it works" page comes from a committed results file
in eval/results/ or bench/results/, written by an eval or bench script. These tests pin down
that no path leads from the simulated rows (scripts/seed_demo_history.py) into those numbers:
the latency benchmark (the only script that reads the ledger) skips them whatever the filters,
the metrics endpoint is unchanged by seeding, no results file names the simulated source, and
no eval or bench module imports the seed script.
"""

import ast
import hashlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert

from app.config import SIMULATED_SOURCE
from app.db.models import deliveries
from app.reconcile.cost import ReconcileConfig
from bench import latency
from scripts.seed_demo_history import seed_history
from tests.pipeline_fakes import T0
from tests.test_ledger_api import LA, REPO, add_delivery, add_event, client_for

RESULT_FILES = sorted(
    [*(REPO / "eval/results").glob("*.json"), *(REPO / "bench/results").glob("*.json")]
)
REPORTED = [REPO / "README.md", *RESULT_FILES]


@pytest.fixture
def media(tmp_path) -> Path:
    p = tmp_path / "media"
    p.mkdir()
    return p


def _digest(paths: list[Path]) -> dict[str, str]:
    return {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


async def _seed(engine, media) -> None:
    await seed_history(
        engine,
        today=date(2026, 10, 17),
        tz=LA,
        media_dir=media,
        now=datetime(2026, 10, 17, 19, 0, tzinfo=UTC),
        cfg=ReconcileConfig(),
    )


async def test_latency_bench_never_reads_simulated_rows(engine, database_url, media):
    await add_delivery(engine, media, T0)
    # Worst case: a simulated row that looks like a pipeline run (an event, stage timings).
    ev = await add_event(engine, T0 + timedelta(minutes=5))
    async with engine.begin() as conn:
        await conn.execute(
            insert(deliveries).values(
                id=uuid.uuid4(),
                event_id=ev,
                detected_at=T0 + timedelta(minutes=5),
                carrier="UPS",
                package_count=1,
                label_text=[],
                latency_ms={"event_to_ledger": 1.0},
                source=SIMULATED_SOURCE,
            )
        )
    await _seed(engine, media)
    rows = await latency.load(database_url, None, None)
    assert rows == [{"event_to_ledger": 12510.7}]  # only the live delivery
    # Asking for simulated rows explicitly returns nothing either.
    assert await latency.load(database_url, SIMULATED_SOURCE, None) == []


def test_latency_bench_cli_does_not_offer_the_simulated_source(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["bench.latency", "--source", SIMULATED_SOURCE])
    with pytest.raises(SystemExit):
        latency.main()
    assert "invalid choice" in capsys.readouterr().err


async def test_seeding_changes_no_reported_number(engine, media):
    before_files = _digest(REPORTED)
    async with client_for(engine, media) as c:
        before = (await c.get("/v1/metrics")).json()
        await _seed(engine, media)
        after = (await c.get("/v1/metrics")).json()
    assert after == before and before["extraction"] and before["reconciliation"]
    assert _digest(REPORTED) == before_files


def _values(obj, key: str):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from _values(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _values(v, key)


@pytest.mark.parametrize("path", RESULT_FILES, ids=lambda p: p.name)
def test_no_results_file_counts_simulated_rows(path):
    data = json.loads(path.read_text())
    assert SIMULATED_SOURCE not in set(map(str, _values(data, "source")))


def test_no_metric_code_imports_the_seed_script():
    modules = [
        *(REPO / "eval").glob("*.py"),
        *(REPO / "bench").glob("*.py"),
        REPO / "app/ledger/metrics.py",
    ]
    for path in modules:
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any("seed_demo_history" in n for n in names), path
