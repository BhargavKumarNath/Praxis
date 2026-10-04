"""No target leakage: a forecast made at origin T cannot depend on anything observed after T.

The check is behavioural: rewrite every outcome dated after T (demand, served, active
customers, regional and external context) and require bit-identical features. A
deliberately leaky builder must fail the same check (negative control), proving the test
can detect leakage. Planned prices are the one whitelisted future input (ADR 0010).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from praxis.forecasting.backtest import TemporalLeakError, make_dataset, rolling_origins, split
from praxis.forecasting.features import (
    WINDOW,
    FeatureFrame,
    build_features,
    feature_names,
)
from praxis.forecasting.panel import DemandPanel, PricePlan
from tests.forecasting.helpers import catalogue, make_panel, make_plan, small_config

HORIZONS = (1, 2, 3, 4, 5, 6, 7)
Builder = Callable[[DemandPanel, int], FeatureFrame]


def rewrite_future(panel: DemandPanel, origin: int, seed: int) -> DemandPanel:
    """Same past, arbitrary future: every outcome after ``origin`` is replaced."""
    rng = np.random.default_rng(seed)
    demand = panel.demand.copy()
    demand[:, origin + 1 :] = rng.integers(0, 10**6, demand[:, origin + 1 :].shape)
    served = panel.served.copy()
    served[:, origin + 1 :] = np.floor(demand[:, origin + 1 :] * rng.random())
    active = panel.active.copy()
    active[:, origin + 1 :] = rng.integers(0, 999, active[:, origin + 1 :].shape)
    context = {}
    for name, arr in panel.context.items():
        new = arr.copy()
        new[:, origin + 1 :] = rng.normal(1e3, 1e3, new[:, origin + 1 :].shape)
        context[name] = new
    return replace(panel, demand=demand, served=served, active=active, context=context)


def honest(external: bool) -> Builder:
    def build(panel: DemandPanel, origin: int) -> FeatureFrame:
        return build_features(panel, origin, HORIZONS, make_plan(), catalogue(), external=external)

    return build


def leaky(panel: DemandPanel, origin: int) -> FeatureFrame:
    """Negative control: appends the realised target y[T + h] as a feature."""
    frame = honest(False)(panel, origin)
    y_target = panel.demand[frame.series_idx, frame.target_day]
    return replace(frame, X=np.column_stack([frame.X, y_target]), names=(*frame.names, "leak"))


def depends_on_future(build: Builder, panel: DemandPanel, origin: int, seed: int) -> bool:
    a = build(panel, origin)
    b = build(rewrite_future(panel, origin, seed), origin)
    return not np.array_equal(a.X, b.X, equal_nan=True) or not np.array_equal(a.scale, b.scale)


PANEL = make_panel(n_days=84)


@settings(max_examples=40, deadline=None)
@given(
    origin=st.integers(min_value=WINDOW - 1, max_value=PANEL.n_days - 8),
    seed=st.integers(0, 2**32 - 1),
    external=st.booleans(),
)
def test_features_never_depend_on_outcomes_after_the_origin(
    origin: int, seed: int, external: bool
) -> None:
    assert not depends_on_future(honest(external), PANEL, origin, seed)


def test_origin_at_panel_end_needs_no_future_at_all() -> None:
    """Serving case: the panel ends at the origin and every horizon still gets features."""
    origin = PANEL.n_days - 1
    frame = honest(True)(PANEL, origin)
    assert len(frame) == len(PANEL.series) * len(HORIZONS)
    assert frame.target_day.min() == origin + 1


@pytest.mark.parametrize("origin", [WINDOW - 1, 50, PANEL.n_days - 8])
def test_negative_control_leaky_feature_is_detected(origin: int) -> None:
    assert depends_on_future(leaky, PANEL, origin, seed=1)


def test_planned_price_is_the_only_future_input_and_only_on_the_target_day() -> None:
    origin = 40
    base = make_plan()
    frame = honest(False)(PANEL, origin)
    target_dates = {PANEL.date_of(int(d)) for d in frame.target_day.tolist()}
    # a price point after the forecast window changes nothing
    later = PricePlan(
        {**base.changes, "p_gpu": (*base.changes["p_gpu"], (PANEL.date_of(origin + 20), 777))}
    )
    same = build_features(PANEL, origin, HORIZONS, later, catalogue())
    assert np.array_equal(frame.X, same.X, equal_nan=True)
    # a planned price inside the window changes only price features of affected rows
    planned = base.with_planned({"p_gpu": 600}, PANEL.date_of(origin + 3))
    moved = build_features(PANEL, origin, HORIZONS, planned, catalogue())
    changed_cols = {
        frame.names[j] for j in np.flatnonzero((frame.X != moved.X).any(axis=0)).tolist()
    }
    assert changed_cols == {"log_price_change_to_target", "log_price_vs_first"}
    rows = np.flatnonzero((frame.X != moved.X).any(axis=1))
    products = {PANEL.series[int(frame.series_idx[r])].product for r in rows}
    assert products == {"p_gpu"}
    assert all(int(frame.horizon[r]) >= 3 for r in rows)
    assert PANEL.date_of(origin + 3) in target_dates


def test_feature_names_whitelist_future_inputs() -> None:
    """Review guard: only calendar, horizon and planned-price features describe day D."""
    names = set(feature_names(external=True))
    future_inputs = {"dow", "horizon", "log_price_change_to_target", "log_price_vs_first"}
    assert future_inputs <= names
    assert not any("target" in n for n in names - {"log_price_change_to_target"})


def test_every_backtest_fold_trains_strictly_before_it_evaluates() -> None:
    panel = make_panel(n_days=112)
    cfg = small_config()
    ds = make_dataset(panel, make_plan(), cfg)
    origins = rolling_origins(panel.n_days, cfg)
    assert origins, "backtest must have at least one origin"
    for origin in origins:
        train, test = split(ds, origin)
        assert ds.frame.target_day[train].max() <= origin
        assert ds.frame.target_day[test].min() > origin
        assert set(ds.frame.origin[test].tolist()) == {origin}
        # every training feature row was itself computed at an origin before the target
        assert (ds.frame.origin[train] < ds.frame.target_day[train]).all()


def test_split_refuses_overlapping_train_and_test() -> None:
    panel = make_panel(n_days=112)
    ds = make_dataset(panel, make_plan(), small_config())
    origin = rolling_origins(panel.n_days, small_config())[0]
    # corrupt the frame: forecasts "made at" the origin now target the origin itself
    horizon = np.where(ds.frame.origin == origin, 0, ds.frame.horizon)
    bad = replace(ds, frame=replace(ds.frame, horizon=horizon))
    with pytest.raises(TemporalLeakError):
        split(bad, origin)


def test_no_random_split_exists_in_the_backtest_api() -> None:
    import praxis.forecasting.backtest as bt

    source = Path(bt.__file__).read_text(encoding="utf-8")
    for forbidden in ("shuffle", "permutation", "train_test_split", "KFold"):
        assert forbidden not in source


def test_history_is_a_copy_not_a_view() -> None:
    hist = PANEL.history(40)
    hist.demand[:] = -1
    assert PANEL.demand.min() >= 0
    assert hist.n_days == 41 and hist.end_date == PANEL.start_date + timedelta(days=40)
