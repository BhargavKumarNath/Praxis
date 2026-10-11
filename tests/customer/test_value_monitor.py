"""Customer values at a decision date, recent calibration, segment monitoring and the CLI."""

from __future__ import annotations

import json
import math
import runpy
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from praxis.customer.__main__ import main
from praxis.customer.artifact import CustomerArtifact, load_artifact
from praxis.customer.clv import lifetime_value
from praxis.customer.config import CustomerValueConfig, load_config
from praxis.customer.dataset import build_panel, snapshot_dates
from praxis.customer.hazard import PoissonHazard, RateTable
from praxis.customer.monitor import segment_report
from praxis.customer.value import customer_values, recent_calibration
from praxis.customer.warehouse import connect
from tests.customer import marts_db
from tests.customer.marts_db import AS_OF

CFG = load_config()
RATES = {"starter": 0.002, "growth": 0.001, "enterprise": 0.0005}


def artifact() -> CustomerArtifact:
    glm = PoissonHazard((), np.zeros(0), np.ones(0), np.zeros(1), np.eye(1), 1)
    return CustomerArtifact(
        {"model_version": "churn-table-test", "champion": "tier_rate_table"},
        glm,
        RateTable(RATES, 0.001, 28.0),
        RateTable({"starter": 0.0003, "growth": 0.0002, "enterprise": 0.0001}, 0.0002, 28.0),
    )


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    return marts_db.build(tmp_path / "wh.duckdb")


def small_config(**monitoring: float) -> CustomerValueConfig:
    raw = CFG.model_dump()
    raw["label"] = {"window_days": 7, "snapshot_step_days": 7, "first_snapshot_day": 14}
    raw["features"] = {
        "spend_window_days": 7,
        "trend_half_days": 3,
        "failure_window_days": 14,
        "service_window_days": 7,
    }
    raw["hazard"] = {**raw["hazard"], "backtest_windows": 2}
    raw["monitoring"] = {**raw["monitoring"], **monitoring}
    return CustomerValueConfig.model_validate(raw)


def test_recent_calibration_compares_observed_with_expected(warehouse: Path) -> None:
    con = connect(warehouse)
    cal = recent_calibration(con, artifact().champion, AS_OF, CFG)
    con.close()
    start = AS_OF - timedelta(days=28)
    assert cal.window_start == start
    # at 2026-06-22: c1 growth, c2 enterprise, c3 starter (churns 07-05 voluntarily: 14 days at
    # risk), c4 growth, c5 starter; one voluntary churner since
    expected = 28 * (0.001 + 0.0005 + 0.001 + 0.002) + 14 * 0.002
    assert cal.observed == 1.0
    assert cal.expected == pytest.approx(expected)
    assert cal.factor == pytest.approx(1.5 / (expected + 0.5))
    assert cal.sd_log == pytest.approx(1 / math.sqrt(1.5))
    assert cal.record()["window_start"] == start.isoformat()


def test_customer_values_apply_calibration_and_value_each_customer(warehouse: Path) -> None:
    con = connect(warehouse)
    v = customer_values(con, artifact(), AS_OF, CFG)
    con.close()
    assert v.snapshot.ids == ("c1", "c2", "c4", "c5")
    assert v.calibration is not None
    factor = v.calibration.factor
    hazard = np.array([RATES["growth"], RATES["enterprise"], RATES["growth"], RATES["starter"]])
    assert v.voluntary_hazard == pytest.approx(hazard * factor)
    margin = v.snapshot.daily_margin_micros() * (1 - v.snapshot.payment_loss_rate)
    assert v.margin_micros == pytest.approx(margin)
    clv = lifetime_value(
        margin, v.voluntary_hazard + v.involuntary_hazard, horizon_days=365, annual_rate=0.10
    )
    assert v.clv_micros == pytest.approx(clv)
    assert v.window_churn() == pytest.approx(1 - np.exp(-28 * v.voluntary_hazard))
    seg = v.segments(("starter", "growth", "enterprise", "none"))
    assert seg["growth"].customers == 2 and seg["enterprise"].customers == 1
    assert seg["growth"].mean_clv_micros == pytest.approx(clv[[0, 2]].mean())
    assert seg["none"].customers == 0 and seg["none"].mean_clv_micros == 0.0
    # enterprise churns least and is worth the most per customer here
    assert seg["enterprise"].mean_clv_micros > seg["growth"].mean_clv_micros
    assert seg["enterprise"].record()["segment"] == "enterprise"


def test_calibration_can_be_disabled(warehouse: Path) -> None:
    raw = CFG.model_dump()
    raw["calibration"] = {"enabled": False}
    cfg = CustomerValueConfig.model_validate(raw)
    con = connect(warehouse)
    v = customer_values(con, artifact(), AS_OF, cfg)
    con.close()
    assert v.calibration is None
    assert v.voluntary_hazard[1] == pytest.approx(RATES["enterprise"])


def test_segment_monitoring_reports_and_alerts(warehouse: Path) -> None:
    con = connect(warehouse)
    quiet = segment_report(con, artifact(), AS_OF, CFG)
    # as of 07-10 the last 7-day window holds c3's voluntary churn (07-05): 1 of 2 starters
    loud = segment_report(
        con, artifact(), date(2026, 7, 10), small_config(min_customers=1, max_window_churn=0.01)
    )
    con.close()
    assert quiet["alerts"] == []  # every segment is below min_customers (20)
    growth = quiet["segments"]["growth"]
    assert growth["value"]["customers"] == 2
    assert growth["price_change_exposure"] == {"0": 1, "2": 1}  # c4 none, c1 two dates
    assert len(growth["clv_sensitivity"]) == len(CFG.clv.sensitivity_discount_rates) * len(
        CFG.clv.sensitivity_horizons_days
    )
    assert 0.0 <= growth["retention_90d"] <= 1.0
    assert quiet["calibration"]["observed"] == 1.0
    kinds = {(a["segment"], a["alert"]) for a in loud["alerts"]}
    assert ("starter", "window_churn") in kinds
    assert loud["segments"]["starter"]["last_window"]["voluntary"] == 1.0
    assert all(a["alert"] in {"window_churn", "calibration_ratio"} for a in loud["alerts"])


def test_panel_from_the_warehouse_has_disjoint_label_windows(warehouse: Path) -> None:
    cfg = small_config()
    dates = snapshot_dates(marts_db.START, AS_OF, cfg.label)
    assert dates == [date(2026, 6, 15), date(2026, 6, 22), date(2026, 6, 29), date(2026, 7, 6),
                     date(2026, 7, 13)]  # fmt: skip
    con = connect(warehouse)
    panel = build_panel(con, dates, cfg)
    con.close()
    # c3 churns voluntarily on 07-05: labelled in the window starting 06-29, gone afterwards
    rows = [i for i, c in enumerate(panel.ids) if c == "c3"]
    assert [panel.dates[panel.window[i]] for i in rows] == dates[:3]
    assert [panel.voluntary[i] for i in rows] == [0.0, 0.0, 1.0]
    assert panel.exposure[rows[2]] == 7.0  # 06-29 .. 07-05
    assert panel.data_version() == panel.data_version()
    with pytest.raises(ValueError, match="no snapshot dates"):
        build_panel(connect(warehouse), [], cfg)


def test_cli_trains_and_monitors(warehouse: Path, tmp_path: Path) -> None:
    cfg_path = tmp_path / "value.toml"
    cfg_path.write_text(_toml(small_config(min_customers=1, max_window_churn=0.01).model_dump()))
    report = tmp_path / "train.json"
    common = ["--db", str(warehouse), "--config", str(cfg_path)]
    assert main([*common, "train", "--start", "2026-06-01", "--cutoff", AS_OF.isoformat(),
                 "--out", str(tmp_path / "models"), "--report", str(report)]) == 0  # fmt: skip
    trained = json.loads(report.read_text())
    path = Path(trained["artifact"])
    assert load_artifact(path).model_version == trained["model_version"]
    out = tmp_path / "monitor.json"
    code = main([*common, "monitor", "--model", str(path), "--as-of", AS_OF.isoformat(),
                 "--out", str(out)])  # fmt: skip
    monitored = json.loads(out.read_text())
    assert code == (1 if monitored["alerts"] else 0)
    assert monitored["model_version"] == trained["model_version"]


def test_entry_point_parses_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["praxis.customer", "--help"])
    monkeypatch.delitem(sys.modules, "praxis.customer.__main__", raising=False)
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("praxis.customer", run_name="__main__")
    assert exc.value.code == 0


def _toml(raw: dict[str, object]) -> str:
    lines: list[str] = []
    for section, body in raw.items():
        assert isinstance(body, dict)
        lines.append(f"[{section}]")
        for k, v in body.items():
            lines.append(f"{k} = {json.dumps(list(v) if isinstance(v, tuple) else v)}")
    return "\n".join(lines) + "\n"


def test_trend_calibration_forecasts_a_rising_hazard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Weekly observed / expected rising ~10% a week: the forecast for the decision week sits
    above the 28-day level (which is centred two weeks back); too few events -> level."""
    from praxis.customer import value as value_mod

    raw = CFG.model_dump()
    raw["calibration"] = {**raw["calibration"], "method": "trend"}
    cfg = CustomerValueConfig.model_validate(raw)
    weeks = cfg.calibration.weeks

    def rising(
        con: object, model: object, start: date, days: int, c: object
    ) -> tuple[float, float]:
        k = (AS_OF - start).days / 7  # 1 = last week
        if days != 7:
            return 40.0, 40.0 * 0.9  # the level window
        return 10.0 * math.exp(-0.1 * (k - 0.5)) * 1.2, 10.0

    monkeypatch.setattr(value_mod, "_window", rising)
    cal = recent_calibration(None, artifact().champion, AS_OF, cfg)  # type: ignore[arg-type]
    assert cal.method == "trend" and cal.trend_per_week > 0
    level = (40.0 + 0.5) / (36.0 + 0.5)  # the 28-day level, centred two weeks back
    assert cal.factor > level > cal.observed / cal.expected  # the trend lifts the forecast
    assert 0 < cal.sd_log < 0.5
    assert cal.window_start == AS_OF - timedelta(days=7 * weeks)

    monkeypatch.setattr(value_mod, "_window", lambda *a: (0.5, 1.0) if a[3] == 7 else (2.0, 2.0))
    assert recent_calibration(None, artifact().champion, AS_OF, cfg).method == "level"  # type: ignore[arg-type]
