"""Metric definitions on hand-calculated fixtures."""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from praxis.forecasting.metrics import (
    bootstrap_vwape_difference,
    coverage,
    pinball,
    sliced,
    summarize,
    vwape,
    weighted_pinball,
)
from praxis.forecasting.models import Prediction

LEVELS = (0.1, 0.25, 0.5, 0.75, 0.9)


def pred(point: list[float], width: float = 1.0) -> Prediction:
    p = np.asarray(point, dtype=float)
    offsets = np.array([-2, -1, 0, 1, 2]) * width
    return Prediction(p, p[:, None] + offsets[None, :], LEVELS, 0)


def test_vwape_by_hand() -> None:
    y = np.array([10.0, 20.0])
    yhat = np.array([12.0, 15.0])
    w = np.array([1.0, 2.0])
    # (1*2 + 2*5) / (1*10 + 2*20) = 12 / 50
    assert vwape(y, yhat, w) == pytest.approx(0.24)
    assert np.isnan(vwape(np.zeros(2), yhat, w))


def test_pinball_by_hand() -> None:
    y = np.array([10.0, 10.0])
    q = np.array([8.0, 12.0])
    np.testing.assert_allclose(pinball(y, q, 0.9), [1.8, 0.2])
    np.testing.assert_allclose(pinball(y, q, 0.1), [0.2, 1.8])


def test_weighted_pinball_is_zero_for_a_perfect_degenerate_forecast() -> None:
    y = np.array([5.0, 7.0])
    assert weighted_pinball(y, pred([5.0, 7.0], width=0.0), np.ones(2)) == 0.0
    assert np.isnan(weighted_pinball(np.zeros(2), pred([1.0, 1.0]), np.ones(2)))


@given(st.lists(st.floats(0, 1e6, allow_nan=False), min_size=1, max_size=30))
def test_pinball_is_non_negative(values: list[float]) -> None:
    y = np.asarray(values)
    for a in LEVELS:
        assert (pinball(y, y[::-1], a) >= 0).all()


def test_coverage_counts_inclusive_bounds() -> None:
    y = np.array([1.0, 2.0, 3.0, 4.0])
    assert coverage(y, np.array([1.0, 3.0, 0.0, 5.0]), np.array([1.0, 4.0, 3.0, 6.0])) == 0.5
    assert np.isnan(coverage(np.array([]), np.array([]), np.array([])))


def test_summarize_reports_every_metric() -> None:
    y = np.array([10.0, 20.0, 30.0, 40.0])
    out = summarize(y, pred([11.0, 19.0, 33.0, 40.0], width=2.0), np.ones(4))
    assert set(out) == {
        "rows",
        "vwape",
        "mae",
        "rmse",
        "bias",
        "pinball",
        "coverage_50",
        "coverage_80",
    }
    assert out["mae"] == pytest.approx(1.25)
    assert out["coverage_80"] == 1.0  # [y-4, y+4] contains every value
    assert out["coverage_50"] == 0.75  # |err| <= 2 for three of four rows


def test_sliced_groups_rows() -> None:
    y = np.array([10.0, 20.0, 30.0])
    out = sliced(y, pred([10.0, 25.0, 30.0]), np.ones(3), {"region": np.array(["a", "b", "a"])})
    assert out["region"]["a"]["vwape"] == 0.0
    assert out["region"]["b"]["vwape"] == pytest.approx(0.25)


def test_block_bootstrap_is_deterministic_and_brackets_the_estimate() -> None:
    rng = np.random.default_rng(0)
    y = rng.gamma(5, 10, 400)
    good = y + rng.normal(0, 2, 400)
    bad = y + rng.normal(0, 10, 400)
    blocks = np.repeat(np.arange(20), 20)
    kw = {"samples": 500, "seed": 7}
    a = bootstrap_vwape_difference(y, good, bad, np.ones(400), blocks, **kw)
    b = bootstrap_vwape_difference(y, good, bad, np.ones(400), blocks, **kw)
    assert a == b
    assert a["ci95_low"] <= a["estimate"] <= a["ci95_high"] < 0
    same = bootstrap_vwape_difference(y, good, good, np.ones(400), blocks, **kw)
    assert same["estimate"] == same["ci95_low"] == same["ci95_high"] == 0.0
