"""Survival model: gradient, censoring, parameter recovery on a known process, persistence."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from praxis.recovery.dataset import Episode, Retry
from praxis.recovery.features import REASONS
from praxis.recovery.survival import CureWeibull, FitError, _design, _weibull, design_names
from tests.recovery.helpers import TRUE, episodes, true_cdf

FIT: dict[str, Any] = {"ridge": 1e-3, "max_iter": 3000, "gtol": 1e-7}


@pytest.fixture(scope="module")
def big() -> list[Episode]:
    return episodes(6000, seed=11)


@pytest.fixture(scope="module")
def fitted(big: list[Episode]) -> CureWeibull:
    return CureWeibull.fit(big, **FIT)


def test_interval_bracketing() -> None:
    e = episodes(1, seed=0)[0]
    rec = Episode(
        e.invoice_id,
        e.customer_id,
        e.failed_at,
        100,
        e.features,
        (Retry(2.0, False), Retry(5.0, True)),
    )
    assert rec.interval == (2.0, 5.0) and rec.recovered
    cens = Episode(
        e.invoice_id,
        e.customer_id,
        e.failed_at,
        100,
        e.features,
        (Retry(2.0, False), Retry(5.0, False)),
    )
    assert cens.interval == (5.0, math.inf) and not cens.recovered
    none = Episode(e.invoice_id, e.customer_id, e.failed_at, 100, e.features, ())
    assert none.interval == (0.0, math.inf) and none.first_retry is None


def test_weibull_edges_are_flat() -> None:
    t = np.array([0.0, math.inf, 3.0])
    cdf, d_scale, d_shape = _weibull(t, np.log(np.full(3, 4.0)), np.zeros(3))
    assert cdf[0] == 0.0 and cdf[1] == 1.0
    assert cdf[2] == pytest.approx(1 - math.exp(-0.75))
    assert d_scale[0] == d_scale[1] == 0.0 and d_shape[0] == d_shape[1] == 0.0


def test_analytic_gradient_matches_finite_differences() -> None:
    eps = episodes(400, seed=3)
    raw_model = CureWeibull.fit(eps, **FIT)
    z, reason = _design([e.features for e in eps], raw_model.mean, raw_model.sd)
    bounds = np.array([e.interval for e in eps])
    rng = np.random.default_rng(0)
    # Around the optimum (random far-off parameters put interval masses near 1e-10, where
    # central differences themselves are inaccurate).
    fitted = np.concatenate([raw_model.b_cure, raw_model.b_scale, raw_model.log_shape])
    theta = fitted + rng.normal(0, 0.1, len(fitted))
    _, grad = raw_model._neg_loglik(theta, z, reason, bounds, 1e-3)
    h = 1e-6
    for j in rng.choice(len(theta), 12, replace=False):
        step = np.zeros_like(theta)
        step[j] = h
        up, _ = raw_model._neg_loglik(theta + step, z, reason, bounds, 1e-3)
        down, _ = raw_model._neg_loglik(theta - step, z, reason, bounds, 1e-3)
        assert grad[j] == pytest.approx((up - down) / (2 * h), rel=1e-4, abs=1e-7)


def test_recovers_the_true_collectible_curve(big: list[Episode], fitted: CureWeibull) -> None:
    """Well-specified world: tight where first retries observe C densely (<= 10 days), looser
    in the tail that only third attempts reach (14-20 days, few rows)."""
    assert fitted.converged and fitted.names == design_names()
    grid = np.array([1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 14.0, 20.0])
    for reason in REASONS:
        rows = [e.features for e in big if e.features.reason == reason]
        err = np.abs(
            fitted.prob_collectible(rows, grid).mean(axis=0)
            - np.array([true_cdf(reason, t) for t in grid])
        )
        assert err[:6].max() < 0.04, (reason, err.round(3))
        assert err[6:].max() < 0.10, (reason, err.round(3))


def test_shapes_are_recovered(fitted: CureWeibull) -> None:
    shapes = dict(zip(REASONS, np.exp(fitted.log_shape), strict=True))
    for reason in ("insufficient_funds", "card_declined", "expired_card"):
        assert shapes[reason] == pytest.approx(TRUE[reason][1], rel=0.2), reason


def test_right_censoring_at_a_cutoff_is_unbiased() -> None:
    """Retries after a cutoff are dropped (as the training extract does): no bias."""
    eps = episodes(6000, seed=12, cutoff_days=250.0)  # the last ~50 days are censored
    model = CureWeibull.fit(eps, **FIT)
    rows = [e.features for e in eps if e.features.reason == "insufficient_funds"]
    pred = model.prob_collectible(rows, np.array([5.0, 10.0])).mean(axis=0)
    true = [true_cdf("insufficient_funds", 5.0), true_cdf("insufficient_funds", 10.0)]
    assert np.max(np.abs(pred - true)) < 0.04


def test_prob_at_matches_the_grid_and_curves_are_monotone(
    big: list[Episode], fitted: CureWeibull
) -> None:
    rows = [e.features for e in big[:50]]
    t = np.linspace(0.5, 20, 50)
    grid = fitted.prob_collectible(rows, t)
    assert np.all(np.diff(grid, axis=1) >= -1e-12)
    np.testing.assert_allclose(fitted.prob_at(rows, t), np.diag(grid))


def test_state_round_trip_and_design_check(fitted: CureWeibull, big: list[Episode]) -> None:
    clone = CureWeibull.from_state(fitted.state())
    rows = [e.features for e in big[:20]]
    t = np.array([1.0, 7.0])
    np.testing.assert_array_equal(clone.prob_collectible(rows, t), fitted.prob_collectible(rows, t))
    bad = {**fitted.state(), "names": ["x"]}
    with pytest.raises(ValueError, match="feature design"):
        CureWeibull.from_state(bad)


def test_fit_refuses_empty_and_reports_non_convergence(big: list[Episode]) -> None:
    with pytest.raises(FitError):
        CureWeibull.fit([], **FIT)
    with pytest.raises(FitError, match="converge"):
        CureWeibull.fit(big[:500], ridge=1e-3, max_iter=10, gtol=1e-12)


def test_fit_is_deterministic(big: list[Episode]) -> None:
    a = CureWeibull.fit(big[:800], **FIT)
    b = CureWeibull.fit(big[:800], **FIT)
    np.testing.assert_array_equal(a.b_cure, b.b_cure)
    np.testing.assert_array_equal(a.log_shape, b.log_shape)
