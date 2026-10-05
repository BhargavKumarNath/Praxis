"""Panel and price-plan invariants; forecast config validation and pre-registration guard."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from praxis.forecasting.config import ForecastConfig, load_forecast_config
from praxis.forecasting.features import Catalogue, build_features
from praxis.forecasting.panel import DemandPanel, PricePlan, SeriesKey
from tests.forecasting.helpers import START, make_panel, make_plan


def test_preregistered_acceptance_thresholds_are_unchanged() -> None:
    """Guard: fixed before any backtest ran (ADR 0010) and re-registered before the
    evaluation world ran (ADR 0011). Loosening them after seeing a result is forbidden; a
    change here needs a new ADR, not a quick edit."""
    acc = load_forecast_config().acceptance
    assert acc.candidate == "hybrid"
    assert acc.max_vwape_ratio_vs_seasonal_naive == 0.90
    assert acc.point_must_beat == ("seasonal_naive", "seasonal_moving_average")
    assert acc.require_bootstrap_ci_below_zero is True
    assert acc.max_pinball_ratio_vs_seasonal_naive == 1.0
    assert acc.pinball_must_beat == ("seasonal_naive", "seasonal_moving_average", "ridge")
    assert acc.coverage_80 == (0.74, 0.86)
    assert acc.coverage_50 == (0.43, 0.57)
    assert acc.max_slice_vwape_ratio_vs_seasonal_naive == 1.10
    assert acc.reproducibility_tolerance == 1e-9


def _raw() -> dict[str, Any]:
    return load_forecast_config().model_dump(mode="json")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["target"].update(horizons=[1, 8]),
        lambda r: r["target"].update(horizons=[0, 1]),
        lambda r: r["target"].update(horizons=[2, 1]),
        lambda r: r["target"].update(horizons=[]),
        lambda r: r["target"].update(quantiles=[0.5, 0.1]),
        lambda r: r["target"].update(quantiles=[0.1, 0.5, 1.0]),
        lambda r: r["target"].update(quantiles=[0.25, 0.5, 0.75]),
        lambda r: r["target"].update(min_history_days=7),
        lambda r: r["serving"].update(stale_max_lag_days=0, fresh_max_lag_days=1),
        lambda r: r["evaluation"].update(value_weights={"api_requests": 0.0}),
        lambda r: r["evaluation"].update(value_weights={}),
        lambda r: r.update(unknown_section={}),
    ],
)
def test_incoherent_config_is_rejected(mutate: Any) -> None:
    raw = _raw()
    mutate(raw)
    with pytest.raises(ValidationError):
        ForecastConfig.model_validate(raw)


def test_config_hash_tracks_content() -> None:
    cfg = load_forecast_config()
    assert cfg.config_hash == load_forecast_config().config_hash
    assert cfg.with_external(True).config_hash != cfg.config_hash


def test_panel_rejects_inconsistent_arrays() -> None:
    p = make_panel(n_days=40)
    with pytest.raises(ValueError, match="served"):
        replace(p, served=p.served[:, :10])
    with pytest.raises(ValueError, match="sorted"):
        replace(p, series=tuple(reversed(p.series)))
    with pytest.raises(ValueError, match="regions"):
        replace(p, regions=("r_east",))
    with pytest.raises(ValueError, match="context"):
        replace(p, context={"avg_utilization": np.zeros((1, 3))})
    with pytest.raises(ValueError, match="served"):
        replace(p, served=p.demand + 1)


def test_panel_dates_history_and_version() -> None:
    p = make_panel(n_days=40)
    assert p.end_date == START + timedelta(days=39)
    assert p.day_of(p.date_of(17)) == 17
    with pytest.raises(IndexError):
        p.history(40)
    v = p.data_version()
    assert v == make_panel(n_days=40).data_version()
    bumped = p.demand.copy()
    bumped[0, 0] += 1
    assert replace(p, demand=bumped).data_version() != v


def test_price_plan_lookup_and_planning() -> None:
    plan = PricePlan(
        {
            "a": ((date(2026, 1, 5), 100), (date(2026, 2, 1), 110)),
            "b": ((date(2026, 1, 9), 5),),
        }
    )
    assert plan.price_on("a", date(2026, 1, 4)) is None
    assert plan.price_on("a", date(2026, 1, 31)) == 100
    assert plan.price_on("a", date(2026, 2, 1)) == 110
    assert plan.price_on("zzz", date(2026, 2, 1)) is None
    planned = plan.with_planned({"a": 120, "c": 7}, date(2026, 1, 20))
    assert planned.price_on("a", date(2026, 2, 5)) == 120  # later scheduled point replaced
    assert planned.price_on("a", date(2026, 1, 19)) == 100
    assert planned.price_on("c", date(2026, 1, 20)) == 7
    assert planned.version() != plan.version()
    with pytest.raises(ValueError):
        PricePlan({"a": ((date(2026, 1, 2), 1), (date(2026, 1, 1), 1))})
    with pytest.raises(ValueError):
        PricePlan({"a": ((date(2026, 1, 2), 0),)})


def test_catalogue_rejects_unknown_series() -> None:
    cat = Catalogue.from_series([SeriesKey("r", "p", "s")])
    assert cat.codes(SeriesKey("r", "p", "s")) == (0, 0, 0)
    with pytest.raises(KeyError, match="catalogue"):
        cat.codes(SeriesKey("r", "p", "other"))


def test_feature_builder_rejects_short_history_and_bad_horizons() -> None:
    p: DemandPanel = make_panel(n_days=60)
    cat = Catalogue.from_series(p.series)
    with pytest.raises(ValueError, match="history"):
        build_features(p, 10, (1,), make_plan(), cat)
    for bad in ((), (0,), (8,)):
        with pytest.raises(ValueError, match="horizons"):
            build_features(p, 40, bad, make_plan(), cat)


def test_missing_prices_and_context_become_neutral_features() -> None:
    p = make_panel(n_days=60)
    frame = build_features(p, 40, (1,), PricePlan({}), Catalogue.from_series(p.series))
    for name in ("log_price_change_to_target", "log_price_change_last_7", "log_price_vs_first"):
        assert (frame.column(name) == 0).all()
    empty = replace(p, context={})
    frame = build_features(empty, 40, (1,), make_plan(), Catalogue.from_series(p.series))
    assert np.isnan(frame.column("ctx_avg_utilization")).all()
