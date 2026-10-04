"""Rolling-origin backtest report and the pre-registered acceptance evaluation."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

import pytest

from praxis.forecasting.backtest import evaluate_acceptance, rolling_origins, run_backtest
from praxis.forecasting.config import ForecastConfig
from praxis.forecasting.models import SeasonalMovingAverage, SeasonalNaive
from tests.forecasting.helpers import START, make_panel, make_plan, small_config

CFG = small_config()
PANEL = make_panel(n_days=112)
SPIKES = {("r_east", "p_api", START + timedelta(days=d)) for d in range(100, 104)}


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    return run_backtest(PANEL, make_plan(), CFG, spike_days=SPIKES)


def test_rolling_origins_are_weekly_and_leave_room_for_every_horizon() -> None:
    origins = rolling_origins(112, CFG)
    assert origins == [69, 76, 83, 90, 97, 104]
    assert origins[-1] + 7 <= 111
    assert rolling_origins(70, CFG) == []


def test_report_covers_every_model_slice_and_comparison(report: dict[str, Any]) -> None:
    assert report["is_synthetic"] is True
    assert report["candidate"] == "hybrid"
    assert set(report["models"]) == {
        "seasonal_naive",
        "seasonal_moving_average",
        "ridge",
        "lightgbm",
        "hybrid",
    }
    n_series = len(PANEL.series)
    assert report["evaluated_rows"] == len(report["origins"]) * n_series * 7
    hybrid = report["models"]["hybrid"]
    assert set(hybrid["slices"]) == {"region", "product", "segment", "horizon", "period"}
    assert set(hybrid["slices"]["horizon"]) == {str(h) for h in range(1, 8)}
    assert hybrid["slices"]["period"]["spike"]["rows"] == report["spike_rows"] > 0
    assert set(report["comparisons"]) == {
        f"hybrid_minus_{m}"
        for m in ("seasonal_naive", "seasonal_moving_average", "ridge", "lightgbm")
    }
    assert report["data_version"] == PANEL.data_version()
    assert report["config_hash"] == CFG.config_hash


def test_hybrid_point_is_ridge_and_quantiles_are_lightgbm(report: dict[str, Any]) -> None:
    m = report["models"]
    assert m["hybrid"]["overall"]["vwape"] == m["ridge"]["overall"]["vwape"]
    assert m["hybrid"]["overall"]["pinball"] == m["lightgbm"]["overall"]["pinball"]
    assert m["hybrid"]["overall"]["coverage_80"] == m["lightgbm"]["overall"]["coverage_80"]
    assert report["comparisons"]["hybrid_minus_ridge"]["estimate"] == 0.0


def test_acceptance_lists_every_preregistered_check(report: dict[str, Any]) -> None:
    names = [c["check"] for c in report["acceptance"]["checks"]]
    assert names[:3] == [
        "vwape_ratio_vs_seasonal_naive",
        "vwape_below_seasonal_naive",
        "vwape_below_seasonal_moving_average",
    ]
    for expected in (
        "bootstrap_ci95_high_vs_naive",
        "pinball_ratio_vs_seasonal_naive",
        "pinball_below_seasonal_naive",
        "pinball_below_seasonal_moving_average",
        "pinball_below_ridge",
        "coverage_80",
        "coverage_50",
    ):
        assert expected in names
    assert any(n.startswith("worst_slice_vwape_ratio") for n in names)
    assert report["acceptance"]["passed"] == all(
        c["passed"] for c in report["acceptance"]["checks"]
    )


def _passing(report: dict[str, Any]) -> dict[str, Any]:
    """A report doctored so that every check passes; tests then break one at a time."""
    r = copy.deepcopy(report)
    m = r["models"]
    for name, vw, pin in (
        ("seasonal_naive", 0.20, 0.06),
        ("seasonal_moving_average", 0.19, 0.05),
        ("ridge", 0.15, 0.045),
        ("hybrid", 0.15, 0.040),
    ):
        m[name]["overall"].update(vwape=vw, pinball=pin)
        for group in ("region", "product", "segment"):
            for res in m[name]["slices"][group].values():
                res["vwape"] = vw
    m["hybrid"]["overall"].update(coverage_80=0.80, coverage_50=0.50)
    r["comparisons"]["hybrid_minus_seasonal_naive"]["ci95_high"] = -0.01
    return r


@pytest.mark.parametrize(
    ("breaker", "failing"),
    [
        (lambda r: r["models"]["hybrid"]["overall"].update(vwape=0.185), "vwape_ratio"),
        (
            lambda r: r["models"]["seasonal_moving_average"]["overall"].update(vwape=0.15),
            "vwape_below_seasonal_moving_average",
        ),
        (
            lambda r: r["comparisons"]["hybrid_minus_seasonal_naive"].update(ci95_high=0.001),
            "bootstrap_ci95_high_vs_naive",
        ),
        (
            lambda r: r["models"]["ridge"]["overall"].update(pinball=0.040),
            "pinball_below_ridge",
        ),
        (lambda r: r["models"]["hybrid"]["overall"].update(coverage_80=0.70), "coverage_80"),
        (lambda r: r["models"]["hybrid"]["overall"].update(coverage_50=0.60), "coverage_50"),
        (
            lambda r: next(iter(r["models"]["hybrid"]["slices"]["region"].values())).update(
                vwape=0.23
            ),
            "worst_slice_vwape_ratio",
        ),
    ],
)
def test_each_preregistered_check_can_fail(
    report: dict[str, Any], breaker: Any, failing: str
) -> None:
    good = _passing(report)
    assert evaluate_acceptance(good, CFG)["passed"], evaluate_acceptance(good, CFG)
    breaker(good)
    result = evaluate_acceptance(good, CFG)
    assert not result["passed"]
    failed = [c["check"] for c in result["checks"] if not c["passed"]]
    assert len(failed) == 1 and failed[0].startswith(failing), failed


def test_backtest_without_the_hybrid_components_skips_acceptance() -> None:
    def baselines_only(cfg: ForecastConfig) -> list[Any]:
        return [SeasonalNaive(cfg.target.quantiles), SeasonalMovingAverage(cfg.target.quantiles)]

    report = run_backtest(PANEL, make_plan(), CFG, model_factory=baselines_only)
    assert set(report["models"]) == {"seasonal_naive", "seasonal_moving_average"}
    assert "acceptance" not in report and report["comparisons"] == {}


def test_too_short_panel_and_missing_weights_are_rejected() -> None:
    with pytest.raises(ValueError, match="too short"):
        run_backtest(make_panel(n_days=72), make_plan(), CFG)
    raw = CFG.model_dump(mode="json")
    raw["evaluation"]["value_weights"] = {"p_api": 1.0}
    with pytest.raises(ValueError, match="value weight"):
        run_backtest(PANEL, make_plan(), ForecastConfig.model_validate(raw))
