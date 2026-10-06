"""End to end on real boundaries: simulator -> DuckDB raw -> dbt marts -> elasticity CLI ->
artifact -> ground-truth evaluation CLI. Small pre-registered worlds (SYNTHETIC); the gate
numbers come from ``make elasticity-*`` at 8,000 customers, not from here.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from praxis.data.config import load_sources_config
from praxis.data.warehouse import Warehouse
from praxis.elasticity.__main__ import main as elasticity_main
from praxis.elasticity.artifact import load_artifact
from praxis.elasticity.config import load_elasticity_config, load_registry
from praxis.elasticity.dataset import build_units
from praxis.elasticity.validity import experiment_checks
from praxis.elasticity.warehouse import WarehouseUnavailable, connect, load_extract
from praxis.science.__main__ import main as science_main
from praxis.simulator.config import load_config
from praxis.simulator.runner import run_simulation

pytestmark = [pytest.mark.integration, pytest.mark.slow]
REPO = Path(__file__).resolve().parents[2]
EVAL = REPO / "configs/simulator/scenarios/elasticity_eval.toml"
DIRTY = REPO / "configs/simulator/scenarios/elasticity_contamination.toml"
CUSTOMERS = 2500


def _build(root: Path, scenario: Path, seed: int) -> Path:
    config = load_config(scenario=scenario).with_overrides(n_customers=CUSTOMERS)
    run_simulation(config, seed, out_dir=root / "sim")
    db = root / "wh.duckdb"
    with Warehouse(db) as wh:
        wh.migrate()
        wh.load_region_locations(load_sources_config().locations)
        wh.load_sim_events(
            root / "sim" / "events.ndjson", json.loads((root / "sim/manifest.json").read_text())
        )
    env = {
        **os.environ,
        "PRAXIS_DUCKDB_PATH": str(db),
        "DBT_TARGET_PATH": str(root / "dbt/target"),
        "DBT_LOG_PATH": str(root / "dbt/logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    done = subprocess.run(  # noqa: S603
        [str(REPO / ".venv/bin/dbt"), "run", "--project-dir", "dbt", "--profiles-dir", "dbt"],
        env=env,
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout[-2000:]
    return db


@pytest.fixture(scope="module")
def clean_world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("elasticity_clean")
    _build(root, EVAL, seed=7)
    return root


def test_cli_end_to_end_on_the_preregistered_world(clean_world: Path) -> None:
    out, models = clean_world / "analysis", clean_world / "models"
    code = elasticity_main(
        [
            "--db",
            str(clean_world / "wh.duckdb"),
            "analyze",
            "--out",
            str(out),
            "--models",
            str(models),
        ]
    )
    report = json.loads((out / "report.json").read_text())
    assert code == 0, report["hierarchical"]["diagnostics"]
    assert report["validity"]["passed"]
    assert len(report["experiments"]) == 5
    assert report["estimates"]["pooled"]["ci_high"] < 0
    assert report["registry_hash"] == load_registry().config_hash
    (artifact_dir,) = models.iterdir()
    assert load_artifact(artifact_dir).manifest["data_version"] == report["data_version"]

    args = ["elasticity", "--analysis", str(out), "--sim", str(clean_world / "sim")]
    science_main([*args, "--scenario", str(EVAL)])  # small world: outcome not asserted
    recovery = json.loads((out / "recovery.json").read_text())
    checks = {c["name"]: c for c in recovery["checks"]}
    assert recovery["mode"] == "recovery" and len(checks) == 8
    assert checks["sign"]["passed"] and checks["validity_gates"]["passed"]
    assert checks["pooled_magnitude"]["detail"]["rel_error"] < 0.15


def test_contaminated_world_is_flagged_on_exactly_the_contaminated_tests(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("elasticity_dirty")
    db = _build(root, DIRTY, seed=7)
    cfg = load_elasticity_config()
    con = connect(db)
    try:
        extract = load_extract(con, load_registry(), cfg)
    finally:
        con.close()
    units, designs, census = build_units(extract, cfg)
    failed = {
        (d.id, c.name)
        for k, d in enumerate(designs)
        for c in experiment_checks(units, k, designs, census[k], cfg.validity)
        if c.gate and not c.passed
    }
    assert failed == {
        ("px-2026-02-api-requests", "contamination"),
        ("px-2026-02-data-transfer", "contamination"),
    }


def test_missing_warehouse_and_marts_are_reported(tmp_path: Path) -> None:
    with pytest.raises(WarehouseUnavailable):
        connect(tmp_path / "nope.duckdb")
    import duckdb

    duckdb.connect(str(tmp_path / "empty.duckdb")).close()
    con = connect(tmp_path / "empty.duckdb")
    try:
        with pytest.raises(WarehouseUnavailable, match="dbt"):
            load_extract(con, load_registry(), load_elasticity_config())
    finally:
        con.close()
