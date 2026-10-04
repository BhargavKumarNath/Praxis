"""Baselines, LightGBM, quantile rearrangement, calibration and reproducibility."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from praxis.forecasting.backtest import make_dataset
from praxis.forecasting.features import build_features
from praxis.forecasting.models import (
    HybridForecaster,
    LightGBMForecaster,
    Prediction,
    ResidualQuantiles,
    RidgeBaseline,
    SeasonalMovingAverage,
    SeasonalNaive,
    _rearrange,
)
from tests.forecasting.helpers import catalogue, make_panel, make_plan, small_config

PANEL = make_panel(n_days=112)
CFG = small_config()
LEVELS = CFG.target.quantiles


def training_data(n_days: int = 112):  # type: ignore[no-untyped-def]
    panel = make_panel(n_days=n_days)
    ds = make_dataset(panel, make_plan(), CFG)
    known = ~np.isnan(ds.target)
    return panel, ds.frame.take(known), ds.target[known], ds.train_weight[known]


def test_seasonal_naive_is_exactly_last_weeks_value() -> None:
    frame = build_features(PANEL, 60, (1, 7), make_plan(), catalogue())
    model = SeasonalNaive(LEVELS)
    model.fit(frame, np.ones(len(frame)), np.ones(len(frame)))
    expected = PANEL.demand[frame.series_idx, frame.target_day - 7]
    np.testing.assert_allclose(model.predict(frame).point, expected, rtol=1e-12)


def test_seasonal_moving_average_is_mean_of_four_same_weekdays() -> None:
    frame = build_features(PANEL, 60, (3,), make_plan(), catalogue())
    model = SeasonalMovingAverage(LEVELS)
    model.fit(frame, np.ones(len(frame)), np.ones(len(frame)))
    d = frame.target_day
    expected = np.mean([PANEL.demand[frame.series_idx, d - 7 * k] for k in (1, 2, 3, 4)], axis=0)
    np.testing.assert_allclose(model.predict(frame).point, expected, rtol=1e-12)


def test_residual_quantiles_are_per_horizon_and_monotone() -> None:
    _, frame, target, weight = training_data()
    model = SeasonalNaive(LEVELS)
    model.fit(frame, target, weight)
    assert sorted(model.residuals.table) == list(CFG.target.horizons)
    for q in model.residuals.table.values():
        assert np.all(np.diff(q) >= 0)
    pred = model.predict(frame)
    assert np.all(np.diff(pred.quantiles, axis=1) >= -1e-9)
    assert pred.quantiles.min() >= 0
    restored = ResidualQuantiles.from_json(LEVELS, model.residuals.to_json())
    np.testing.assert_array_equal(restored.apply(frame, frame.column("lag_dow_1")), pred.quantiles)


def test_ridge_recovers_a_known_linear_signal() -> None:
    _, frame, _, weight = training_data()
    signal = 0.5 + 0.8 * frame.column("sma_dow_4")
    model = RidgeBaseline(LEVELS, alpha=1e-6)
    model.fit(frame, signal, weight)
    np.testing.assert_allclose(model.predict(frame).point, signal * frame.scale, rtol=1e-6)


def test_ridge_handles_missing_and_constant_features() -> None:
    _, frame, target, weight = training_data()
    X = frame.X.copy()
    X[:, frame.names.index("ctx_avg_utilization")] = np.nan
    X[:, frame.names.index("log_price_change_last_7")] = 0.0
    nan_frame = replace(frame, X=X)
    model = RidgeBaseline(LEVELS, alpha=1.0)
    model.fit(nan_frame, target, weight)
    assert np.isfinite(model.predict(nan_frame).point).all()


def test_rearrangement_sorts_and_counts_crossing_rows() -> None:
    raw = np.array([[1.0, 2.0, 3.0], [3.0, 2.0, 1.0], [1.0, 1.0, 0.5]])
    ordered, crossed = _rearrange(raw)
    assert crossed == 2
    assert np.all(np.diff(ordered, axis=1) >= 0)


def test_lightgbm_quantiles_are_ordered_and_point_is_finite() -> None:
    _, frame, target, weight = training_data()
    model = LightGBMForecaster(CFG.lightgbm, LEVELS)
    model.fit(frame, target, weight)
    pred = model.predict(frame)
    assert isinstance(pred, Prediction)
    assert np.isfinite(pred.point).all() and pred.point.min() >= 0
    assert np.all(np.diff(pred.quantiles, axis=1) >= 0)
    assert 0 <= pred.raw_crossing_rows <= len(frame)
    assert pred.quantile(0.5).shape == (len(frame),)


def test_lightgbm_training_is_bit_reproducible() -> None:
    _, frame, target, weight = training_data()
    a, b = (LightGBMForecaster(CFG.lightgbm, LEVELS) for _ in range(2))
    a.fit(frame, target, weight)
    b.fit(frame, target, weight)
    assert a.model_strings() == b.model_strings()
    np.testing.assert_array_equal(a.offsets, b.offsets)
    pa, pb = a.predict(frame), b.predict(frame)
    np.testing.assert_array_equal(pa.point, pb.point)
    np.testing.assert_array_equal(pa.quantiles, pb.quantiles)


def test_calibration_widens_intervals_of_an_overfitting_quantile_model() -> None:
    """In-sample quantile fits of a flexible model are too narrow; offsets must widen them."""
    panel = make_panel(n_days=112, noise=0.3)
    ds = make_dataset(panel, make_plan(), CFG)
    known = ~np.isnan(ds.target)
    params = small_config(
        lightgbm={"num_boost_round": 200, "num_leaves": 63, "min_data_in_leaf": 2}
    ).lightgbm
    model = LightGBMForecaster(params, LEVELS)
    model.fit(ds.frame.take(known), ds.target[known], ds.train_weight[known])
    assert model.offsets.shape == (len(LEVELS),)
    assert model.offsets[0] < 0 < model.offsets[-1]
    width = model.offsets[-1] - model.offsets[0]
    assert width > 0


def test_calibration_is_skipped_when_disabled_or_too_little_data() -> None:
    _, frame, target, weight = training_data()
    off = small_config(lightgbm={"calibration_days": 0}).lightgbm
    model = LightGBMForecaster(off, LEVELS)
    model.fit(frame, target, weight)
    assert not model.offsets.any()
    tiny = frame.take(np.flatnonzero(frame.origin >= frame.origin.max() - 3))
    model = LightGBMForecaster(CFG.lightgbm, LEVELS)
    model.fit(tiny, target[frame.origin >= frame.origin.max() - 3], weight[: len(tiny)])
    assert not model.offsets.any()


def test_lightgbm_round_trips_through_model_strings() -> None:
    _, frame, target, weight = training_data()
    model = LightGBMForecaster(CFG.lightgbm, LEVELS)
    model.fit(frame, target, weight)
    again = LightGBMForecaster.from_model_strings(
        CFG.lightgbm, LEVELS, model.model_strings(), model.offsets.tolist()
    )
    np.testing.assert_array_equal(again.predict(frame).quantiles, model.predict(frame).quantiles)
    with pytest.raises(ValueError, match="do not match"):
        LightGBMForecaster.from_model_strings(CFG.lightgbm, LEVELS, {"point": "x"}, [0.0] * 7)
    with pytest.raises(ValueError, match="offset"):
        LightGBMForecaster.from_model_strings(CFG.lightgbm, LEVELS, model.model_strings(), [0.0])


def test_untrained_lightgbm_refuses_to_predict() -> None:
    frame = build_features(PANEL, 60, (1,), make_plan(), catalogue())
    with pytest.raises(RuntimeError, match="not trained"):
        LightGBMForecaster(CFG.lightgbm, LEVELS).predict(frame)


def test_lightgbm_learns_the_weekly_pattern_better_than_naive() -> None:
    """Sanity on a low-noise world: the model should beat y[D-7] out of sample."""
    panel = make_panel(n_days=140, noise=0.15)
    ds = make_dataset(panel, make_plan(), CFG)
    known = ~np.isnan(ds.target)
    origin = 125
    train = known & (ds.frame.target_day <= origin)
    test = known & (ds.frame.origin == origin)
    model = LightGBMForecaster(small_config(lightgbm={"num_boost_round": 150}).lightgbm, LEVELS)
    model.fit(ds.frame.take(train), ds.target[train], ds.train_weight[train])
    naive = SeasonalNaive(LEVELS)
    naive.fit(ds.frame.take(train), ds.target[train], ds.train_weight[train])
    y = ds.y[test]
    err_model = np.abs(model.predict(ds.frame.take(test)).point - y).sum()
    err_naive = np.abs(naive.predict(ds.frame.take(test)).point - y).sum()
    assert err_model < err_naive


def test_quantile_only_lightgbm_matches_the_full_models_quantiles() -> None:
    _, frame, target, weight = training_data()
    full = LightGBMForecaster(CFG.lightgbm, LEVELS)
    q_only = LightGBMForecaster(CFG.lightgbm, LEVELS, with_point=False)
    full.fit(frame, target, weight)
    q_only.fit(frame, target, weight)
    assert "point" not in q_only.boosters
    np.testing.assert_array_equal(full.predict(frame).quantiles, q_only.predict_quantiles(frame)[0])
    with pytest.raises(RuntimeError, match="no point forecast"):
        q_only.predict(frame)


def test_hybrid_fit_equals_composing_ridge_and_lightgbm_predictions() -> None:
    """The backtest composes fold predictions instead of refitting; prove that is exact."""
    _, frame, target, weight = training_data()
    hybrid = HybridForecaster.build(CFG.lightgbm, LEVELS, CFG.ridge.alpha)
    hybrid.fit(frame, target, weight)
    ridge = RidgeBaseline(LEVELS, CFG.ridge.alpha)
    full = LightGBMForecaster(CFG.lightgbm, LEVELS)
    ridge.fit(frame, target, weight)
    full.fit(frame, target, weight)
    composed = HybridForecaster.combine(ridge.predict(frame), full.predict(frame))
    direct = hybrid.predict(frame)
    np.testing.assert_array_equal(direct.point, composed.point)
    np.testing.assert_array_equal(direct.quantiles, composed.quantiles)
    assert direct.raw_crossing_rows == composed.raw_crossing_rows


def test_hybrid_rejects_a_lightgbm_component_with_a_point_model() -> None:
    with pytest.raises(ValueError, match="quantile-only"):
        HybridForecaster(RidgeBaseline(LEVELS, 1.0), LightGBMForecaster(CFG.lightgbm, LEVELS))


def test_ridge_state_round_trips_exactly() -> None:
    _, frame, target, weight = training_data()
    ridge = RidgeBaseline(LEVELS, 1.0)
    with pytest.raises(RuntimeError, match="not trained"):
        ridge.state()
    ridge.fit(frame, target, weight)
    again = RidgeBaseline.from_state(LEVELS, ridge.state())
    np.testing.assert_array_equal(again.predict(frame).point, ridge.predict(frame).point)
    np.testing.assert_array_equal(again.predict(frame).quantiles, ridge.predict(frame).quantiles)
