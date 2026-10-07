"""End to end on real boundaries: simulator -> DuckDB raw -> dbt marts (incl. the marginal-cost
mart) -> forecast artifact trained on the world's prefix -> pricing CLI (shadow in memory,
execute against Postgres) -> shadow evaluation CLI against simulator truth.

A small world (SYNTHETIC); the gate numbers come from ``make pricing-shadow`` at 1,000
customers, not from here. This test proves the wiring and the safety properties.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from datetime import date, timedelta
from pathlib import Path

import pytest

from praxis.data.config import load_sources_config
from praxis.data.warehouse import Warehouse
from praxis.forecasting.artifact import save_artifact, train_artifact
from praxis.forecasting.config import load_forecast_config
from praxis.forecasting.warehouse import connect, load_panel, load_price_plan
from praxis.pricing.__main__ import main as pricing_main
from praxis.science.__main__ import main as science_main
from praxis.simulator.config import load_config
from praxis.simulator.runner import run_simulation
from tests.pricing.fakes import write_artifact, write_report

pytestmark = [pytest.mark.integration, pytest.mark.slow]
REPO = Path(__file__).resolve().parents[2]
SCENARIO = REPO / "configs/simulator/scenarios/pricing_shadow.toml"
CUSTOMERS = 200
PREFIX_DAYS = 196


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("pricing_world")
    config = load_config(scenario=SCENARIO).with_overrides(n_customers=CUSTOMERS)
    run_simulation(config, 5, out_dir=root / "sim")
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
        [str(REPO / ".venv/bin/dbt"), "build", "--project-dir", "dbt", "--profiles-dir", "dbt"],
        env=env,
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert done.returncode == 0, done.stdout[-3000:]
    # forecast artifact trained on the shared prefix only (days 0..195)
    start = config.run.start_date
    con = connect(db)
    panel = load_panel(con, start, start + timedelta(days=PREFIX_DAYS - 1))
    plan = load_price_plan(con, until=start + timedelta(days=PREFIX_DAYS - 1))
    con.close()
    manifest, files = train_artifact(panel, plan, load_forecast_config(), code_revision="test")
    save_artifact(manifest, files, root / "models/demand")
    write_artifact(root / "models/elasticity")
    write_report(root / "report.json")
    return root


def _decide(world: Path, *extra: str) -> list[str]:
    return [
        "decide",
        "--db",
        str(world / "wh.duckdb"),
        "--forecast-model",
        str(next((world / "models/demand").iterdir())),
        "--elasticity-model",
        str(world / "models/elasticity"),
        "--elasticity-report",
        str(world / "report.json"),
        *extra,
    ]


def test_marginal_cost_mart_is_built_and_tested(world: Path) -> None:
    con = connect(world / "wh.duckdb")
    rows = con.execute(
        "SELECT count(DISTINCT product), min(hours), max(hours), min(avg_cost_micros) "
        "FROM marts.fct_marginal_cost_daily"
    ).fetchone()
    con.close()
    assert rows is not None
    products, low, high, cost = rows
    assert (products, low, high) == (5, 24, 24) and cost > 0


def test_shadow_cycle_cli(world: Path, tmp_path: Path) -> None:
    out = tmp_path / "cycle.json"
    code = pricing_main(_decide(world, "--as-of", "2026-07-20", "--out", str(out)))
    assert code == 0
    payload = json.loads(out.read_text())
    assert payload["mode"] == "shadow" and payload["audit_complete"]
    assert len(payload["records"]) == 5
    assert all(not d["executable"] and not d["executed"] for d in payload["decisions"])
    statuses = {d["status"] for d in payload["decisions"]}
    assert statuses <= {"change", "hold", "frozen", "infeasible", "unavailable"}
    for rec in payload["records"]:
        if rec["status"] != "unavailable":
            assert rec["lineage"]["forecast_freshness"] == "fresh"
            assert rec["lineage"]["forecast_feature_date"] == "2026-07-19"  # data before T only


def test_non_shadow_modes_need_the_audit_store(world: Path) -> None:
    with pytest.raises(SystemExit, match="database-url"):
        pricing_main(_decide(world, "--as-of", "2026-07-20", "--mode", "recommend"))


@pytest.mark.usefixtures("world")
def test_execute_and_recommend_through_postgres(world: Path, pg_url: str, tmp_path: Path) -> None:
    raw = tomllib.loads((REPO / "configs/pricing/policy.toml").read_text())
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(_toml(raw))
    # disabled by the policy: refused
    assert pricing_main(
        _decide(world, "--as-of", "2026-07-20", "--mode", "execute", "--database-url", pg_url,
                "--policy", str(policy_path))
    ) == 2  # fmt: skip
    raw["policy"]["allow_execute"] = True
    policy_path.write_text(_toml(raw))
    out = tmp_path / "exec.json"
    code = pricing_main(
        _decide(world, "--as-of", "2026-07-27", "--mode", "execute", "--database-url", pg_url,
                "--policy", str(policy_path), "--out", str(out))
    )  # fmt: skip
    assert code == 0
    decisions = json.loads(out.read_text())["decisions"]
    for d in decisions:
        assert d["executed"] == (d["status"] == "change")
        assert (
            pricing_main(["show", "--database-url", pg_url, "--decision-id", d["decision_id"]]) == 0
        )
    # recommend: approval then execution, by decision id
    rec_out = tmp_path / "rec.json"
    pricing_main(
        _decide(world, "--as-of", "2026-08-03", "--mode", "recommend", "--database-url", pg_url,
                "--out", str(rec_out))
    )  # fmt: skip
    for d in json.loads(rec_out.read_text())["decisions"]:
        args = ["--database-url", pg_url, "--decision-id", d["decision_id"]]
        if d["status"] == "change":
            assert pricing_main(["execute", *args]) == 2  # no approval yet
            assert pricing_main(["approve", *args, "--approver", "pricing-lead"]) == 0
            assert pricing_main(["execute", *args]) == 0
        else:
            assert pricing_main(["approve", *args, "--approver", "pricing-lead"]) == 2
    assert pricing_main(["show", "--database-url", pg_url, "--decision-id", "dec-missing"]) == 2


def test_shadow_evaluation_cli(world: Path, tmp_path: Path) -> None:
    out = tmp_path / "shadow"
    code = science_main(
        [
            "pricing-shadow",
            "--db", str(world / "wh.duckdb"),
            "--sim", str(world / "sim"),
            "--scenario", str(SCENARIO),
            "--forecast-models", str(world / "models/demand"),
            "--elasticity-model", str(world / "models/elasticity"),
            "--elasticity-report", str(world / "report.json"),
            "--out", str(out),
        ]
    )  # fmt: skip
    report = json.loads((out / "shadow.json").read_text())
    assert code == (0 if report["passed"] else 1)
    assert report["decisions"] == 40 and len(report["cycles"]) == 8
    checks = {c["name"]: c["passed"] for c in report["checks"]}
    # safety properties hold on any world; the statistical checks are judged at full size
    for name in ("constraint_compliance", "no_shadow_execution", "complete_records",
                 "forecast_lineage", "stress_fail_safe"):  # fmt: skip
        assert checks[name], name
    assert len((out / "decisions.jsonl").read_text().splitlines()) == 40
    assert {s["case"] for s in report["stress"]} == {
        "stale_features",
        "features_too_old",
        "forecast_model_unavailable",
        "evidence_unavailable",
        "inflated_uncertainty",
    }


def test_shadow_evaluation_refuses_a_forecast_from_another_world(
    world: Path, tmp_path: Path
) -> None:
    other = tmp_path / "models"
    other.mkdir()
    code = science_main(
        [
            "pricing-shadow",
            "--db", str(world / "wh.duckdb"),
            "--sim", str(world / "sim"),
            "--scenario", str(SCENARIO),
            "--forecast-models", str(other),
            "--elasticity-model", str(world / "models/elasticity"),
            "--elasticity-report", str(world / "report.json"),
            "--out", str(tmp_path / "out"),
        ]
    )  # fmt: skip
    assert code == 1


def _toml(raw: dict[str, object]) -> str:
    lines: list[str] = []
    for section, body in raw.items():
        assert isinstance(body, dict)
        if section == "products":
            for name, bounds in body.items():
                lines.append(f"[products.{name}]")
                lines += [f"{k} = {v}" for k, v in bounds.items()]
            continue
        lines.append(f"[{section}]")
        for k, v in body.items():
            lines.append(f"{k} = {json.dumps(v)}")
    return "\n".join(lines) + "\n"


def test_toml_writer_round_trips() -> None:
    raw = tomllib.loads((REPO / "configs/pricing/policy.toml").read_text())
    assert tomllib.loads(_toml(raw)) == raw
    assert date(2026, 7, 20).weekday() == 0  # cycles start on Mondays
