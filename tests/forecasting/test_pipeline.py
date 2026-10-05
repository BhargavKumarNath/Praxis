"""End to end on real boundaries: simulator -> DuckDB raw -> dbt marts -> panel -> backtest ->
artifact -> service. A small scenario world (SYNTHETIC); the gate numbers come from
``make forecast-backtest`` on the pre-registered evaluation world, not from here.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from praxis.data.config import load_sources_config
from praxis.data.warehouse import Warehouse
from praxis.forecasting.artifact import load_artifact, save_artifact, train_artifact
from praxis.forecasting.backtest import run_backtest
from praxis.forecasting.service import ForecastService, Freshness, Source, WarehouseFeatureSource
from praxis.forecasting.warehouse import connect, load_panel, load_price_plan, usage_date_range
from praxis.simulator.config import load_config
from praxis.simulator.runner import run_simulation
from tests.forecasting.helpers import small_config

pytestmark = [pytest.mark.integration, pytest.mark.slow]
REPO = Path(__file__).resolve().parents[2]
SCENARIO = REPO / "configs/simulator/scenarios/forecast_eval.toml"


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("forecast_pipeline")
    config = load_config(scenario=SCENARIO).with_overrides(n_customers=300, days=126)
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
def data(warehouse: Path) -> dict[str, Any]:
    con = connect(warehouse)
    start, end = usage_date_range(con)  # type: ignore[misc]
    out = {"panel": load_panel(con, start, end), "plan": load_price_plan(con, until=end)}
    raw = con.execute(
        """
        SELECT sum((payload->>'units')::BIGINT + (payload->>'throttled_units')::BIGINT)
        FROM raw.sim_events WHERE event_type = 'usage.observed'
        """
    ).fetchone()
    out["raw_demand"] = raw[0] if raw else None
    out["view"] = con.execute(
        "SELECT region_id, feature_date, avg_utilization, max_utilization, avg_error_rate, "
        "avg_latency_p95_ms FROM marts.feat_region_daily"
    ).fetchall()
    con.close()
    return out


def test_panel_from_dbt_marts_matches_raw_events(data: dict[str, Any]) -> None:
    panel = data["panel"]
    assert panel.n_days == 126
    assert {s.segment for s in panel.series} == {"starter", "growth", "enterprise"}
    assert not any(
        s.region_id == "ap_southeast" and s.product == "gpu_minutes" for s in panel.series
    )
    assert float(panel.demand.sum()) == pytest.approx(float(data["raw_demand"]))
    assert np.isfinite(panel.context["avg_utilization"]).all()
    # the loader's exact (DECIMAL) service context equals the dbt feature view's definition
    names = ("avg_utilization", "max_utilization", "avg_error_rate", "avg_latency_p95_ms")
    for region, d, *values in data["view"]:
        r, day = panel.regions.index(region), panel.day_of(d)
        for name, v in zip(names, values, strict=True):
            assert panel.context[name][r, day] == pytest.approx(v, rel=1e-12, abs=1e-15)
    # the scheduled cpu price change (day 70, x1.10) is visible in the plan
    cpu = data["plan"].changes["cpu_minutes"]
    assert cpu[1] == (panel.date_of(70), round(cpu[0][1] * 1.10))


def test_backtest_artifact_and_service_end_to_end(warehouse: Path, data: dict[str, Any]) -> None:
    cfg = small_config(
        backtest={"initial_train_days": 84, "step_days": 14},
        evaluation={
            "value_weights": {
                "api_requests": 0.40,
                "cpu_minutes": 0.012,
                "gpu_minutes": 0.045,
                "data_transfer_gb": 0.07,
                "premium_latency": 0.90,
            }
        },
    )
    panel, plan = data["panel"], data["plan"]
    report = run_backtest(panel, plan, cfg)
    m = report["models"]
    # directional sanity on a small world, not the gate
    assert m["hybrid"]["overall"]["vwape"] < m["seasonal_naive"]["overall"]["vwape"]
    assert 0.6 <= m["hybrid"]["overall"]["coverage_80"] <= 0.95
    manifest, files = train_artifact(panel, plan, cfg, code_revision="it", backtest=report)
    artifact = load_artifact(save_artifact(manifest, files, warehouse.parent / "models"))
    now = datetime.combine(panel.end_date + timedelta(days=1), time(5), tzinfo=UTC)
    svc = ForecastService(artifact, WarehouseFeatureSource(warehouse), clock=lambda: now)
    result = svc.forecast()
    assert result.freshness is Freshness.FRESH and result.source is Source.MODEL
    assert result.feature_date == panel.end_date
    assert len(result.points) == len(panel.series) * 7
    assert all(p.point >= 0 for p in result.points)
