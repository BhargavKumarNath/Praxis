"""CLI: backtest -> train (guarded by acceptance) -> predict, on a real DuckDB file."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from praxis.forecasting import __main__ as cli
from praxis.forecasting.config import ForecastConfig
from tests.forecasting.helpers import START, make_panel, make_plan, small_config, write_warehouse

PANEL = make_panel(n_days=112)
SCENARIO = Path(__file__).resolve().parents[2] / "configs/simulator/scenarios/forecast_eval.toml"


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "wh.duckdb"
    write_warehouse(path, PANEL, make_plan())

    def fast_config(path: Path | None = None) -> ForecastConfig:
        del path
        return small_config()

    monkeypatch.setattr(cli, "load_forecast_config", fast_config)
    return path


def run(db: Path, *args: str) -> int:
    return cli.main(["--db", str(db), *args])


def passing_report(tmp_path: Path, db: Path, **override: Any) -> Path:
    """A real backtest report with acceptance forced to pass (or overridden fields)."""
    out = tmp_path / "bt.json"
    run(db, "backtest", "--report", str(out))
    report = json.loads(out.read_text())
    report["acceptance"]["passed"] = True
    report.update(override)
    out.write_text(json.dumps(report))
    return out


def test_backtest_writes_a_report_and_exit_code_follows_acceptance(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "bt.json"
    code = run(db, "backtest", "--scenario", str(SCENARIO), "--report", str(out))
    report = json.loads(out.read_text())
    assert code == (0 if report["acceptance"]["passed"] else 1)
    assert report["code_revision"]
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert set(summary) == {"overall", "acceptance"}


def test_train_saves_a_reproducible_artifact_then_predict_serves_it(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = passing_report(tmp_path, db)
    capsys.readouterr()
    models = tmp_path / "models"
    assert run(db, "train", "--backtest-report", str(report), "--out", str(models)) == 0
    trained = json.loads(capsys.readouterr().out)
    assert trained["reproducible"] is True
    saved = [p for p in models.iterdir() if p.is_dir()]
    assert len(saved) == 1 and saved[0].name.startswith("demand-hybrid-")
    manifest = json.loads((saved[0] / "manifest.json").read_text())
    assert manifest["backtest"]["acceptance"]["passed"] is True
    assert manifest["data_version"] == cli._load(db)[0].data_version()  # as read from the marts
    capsys.readouterr()
    out = tmp_path / "pred.json"
    as_of = (PANEL.end_date + timedelta(days=1)).isoformat()
    assert run(db, "predict", "--model", str(saved[0]), "--as-of", as_of, "--out", str(out)) == 0
    pred = json.loads(out.read_text())
    assert pred["model_version"] == manifest["model_version"]
    assert pred["freshness"] == "fresh" and pred["source"] == "model"
    assert pred["points"] == len(PANEL.series) * 7
    assert len(pred["total_point_by_target_date"]) == 7
    assert pred["metrics"]["counters"]["forecast_outcome_total:served"] == 1
    capsys.readouterr()
    expired = (PANEL.end_date + timedelta(days=30)).isoformat()
    assert run(db, "predict", "--model", str(saved[0]), "--as-of", expired) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "features_unavailable"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"acceptance": {"passed": False, "checks": []}}, "did not pass"),
        ({"config_hash": "other"}, "different forecast config"),
        ({"data_version": "panel-other"}, "different data"),
    ],
)
def test_train_refuses_an_unvalidated_model(
    db: Path, tmp_path: Path, override: dict[str, Any], message: str
) -> None:
    report = passing_report(tmp_path, db, **override)
    with pytest.raises(SystemExit, match=message):
        run(db, "train", "--backtest-report", str(report), "--out", str(tmp_path / "m"))
    assert not (tmp_path / "m").exists()


def test_train_without_a_report_needs_an_explicit_override(db: Path, tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="refusing"):
        run(db, "train", "--out", str(tmp_path / "m"))
    assert run(db, "train", "--allow-unvalidated", "--out", str(tmp_path / "m")) == 0
    manifest = json.loads(next((tmp_path / "m").glob("*/manifest.json")).read_text())
    assert manifest["backtest"] is None


def test_empty_warehouse_is_a_clean_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import duckdb

    path = tmp_path / "empty.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA marts")
    con.execute("CREATE TABLE marts.fct_usage_daily (event_date DATE)")
    con.close()
    with pytest.raises(SystemExit, match="no usage"):
        run(path, "backtest")


def test_spike_days_follow_the_scenario_file() -> None:
    days = cli.spike_days_from_scenario(SCENARIO)
    # three all-product spikes (6, 4 and 8 days x 5 products; labels are product-agnostic)
    # plus four single-product spikes (5, 5, 8 and 4 days)
    assert ("us_east", "api_requests", START + timedelta(days=95)) in days
    assert ("us_east", "api_requests", START + timedelta(days=100)) not in days  # end exclusive
    assert ("eu_west", "gpu_minutes", START + timedelta(days=40)) in days
    assert len(days) == 6 * 5 + 4 * 5 + 8 * 5 + 5 + 5 + 8 + 4
