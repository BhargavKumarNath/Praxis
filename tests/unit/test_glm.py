"""Shared Poisson solver (praxis.glm): exact cases, recovery on seeded data, failures."""

from __future__ import annotations

import math

import numpy as np
import pytest

from praxis.glm import FitError, fit_poisson


def test_an_intercept_only_fit_is_the_overall_log_rate() -> None:
    events = np.array([0.0, 1.0, 2.0, 0.0])
    exposure = np.array([10.0, 20.0, 30.0, 40.0])
    fit = fit_poisson(np.ones((4, 1)), events, exposure)
    assert fit.coef[0] == pytest.approx(math.log(3 / 100))
    # information = sum(mu) = number of events: SE = 1 / sqrt(3)
    assert math.sqrt(fit.cov[0, 0]) == pytest.approx(1 / math.sqrt(3))
    assert fit.iterations >= 1 and math.isfinite(fit.log_likelihood)


def test_recovers_known_coefficients_from_seeded_data() -> None:
    rng = np.random.default_rng(7)
    n = 40_000
    x = rng.normal(size=n)
    exposure = rng.uniform(5, 30, size=n)
    events = rng.poisson(exposure * np.exp(-5.0 + 0.4 * x)).astype(np.float64)
    design = np.column_stack([np.ones(n), x])
    fit = fit_poisson(design, events, exposure)
    se = np.sqrt(np.diag(fit.cov))
    assert abs(fit.coef[0] + 5.0) < 4 * se[0]
    assert abs(fit.coef[1] - 0.4) < 4 * se[1]
    # the model is correct, so the sandwich agrees with the model-based SE
    assert np.sqrt(np.diag(fit.cov_robust)) == pytest.approx(se, rel=0.15)


def test_a_ridge_penalty_shrinks_the_slope() -> None:
    rng = np.random.default_rng(3)
    x = rng.normal(size=2000)
    events = rng.poisson(10 * np.exp(-2.0 + 0.5 * x)).astype(np.float64)
    design = np.column_stack([np.ones(2000), x])
    free = fit_poisson(design, events, np.full(2000, 10.0))
    shrunk = fit_poisson(design, events, np.full(2000, 10.0), penalty=np.array([0.0, 1e4]))
    assert 0 < shrunk.coef[1] < free.coef[1]


@pytest.mark.parametrize(
    ("design", "events", "exposure", "kwargs", "message"),
    [
        (np.ones((0, 1)), np.zeros(0), np.zeros(0), {}, "person-time"),
        (np.ones((2, 1)), np.zeros(2), np.array([1.0, 0.0]), {}, "person-time"),
        (np.ones((3, 2)), np.ones(3), np.ones(3), {}, "singular"),
        (np.ones((3, 1)), np.array([1e6, 0, 0]), np.ones(3), {"max_iter": 1}, "convergence"),
    ],
)  # fmt: skip
def test_unfittable_inputs_fail_explicitly(
    design: np.ndarray,
    events: np.ndarray,
    exposure: np.ndarray,
    kwargs: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(FitError, match=message):
        fit_poisson(design, events, exposure, **kwargs)  # type: ignore[arg-type]


def test_divergence_is_detected() -> None:
    # perfectly separating covariate: the slope runs off to infinity
    x = np.array([0.0, 0.0, 1.0, 1.0])
    events = np.array([0.0, 0.0, 5.0, 5.0])
    with pytest.raises(FitError):
        fit_poisson(np.column_stack([np.ones(4), x]), events, np.ones(4), max_iter=500)
