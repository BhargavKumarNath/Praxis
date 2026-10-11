"""CLV on hand-calculated fixtures (required_test.md s15: CLV, discount / horizon sensitivity)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from praxis.customer.clv import (
    daily_discount,
    expected_days,
    hazard_from_window,
    lifetime_value,
    sensitivity,
    window_probability,
)


def clv(m: float, h: float, *, horizon: int, rate: float) -> float:
    return float(
        lifetime_value(np.array([m]), np.array([h]), horizon_days=horizon, annual_rate=rate)[0]
    )


def test_no_churn_no_discount_is_margin_times_horizon() -> None:
    assert clv(10.0, 0.0, horizon=365, rate=0.0) == pytest.approx(3650.0)


def test_halving_survival_by_hand() -> None:
    # h = ln 2: S(1) = 1/2, S(2) = 1/4, S(3) = 1/8 -> 0.875 days of margin, no discounting
    assert clv(100.0, math.log(2.0), horizon=3, rate=0.0) == pytest.approx(87.5)
    # one day only: margin x discount x survival
    d = 1.1 ** (-1 / 365)
    assert clv(100.0, math.log(2.0), horizon=1, rate=0.1) == pytest.approx(100.0 * d * 0.5)


def test_closed_form_equals_the_explicit_sum() -> None:
    m, h, r, horizon = 37.5, 0.0013, 0.10, 365
    d = (1 + r) ** (-1 / 365)
    explicit = sum(m * d**t * math.exp(-h * t) for t in range(1, horizon + 1))
    assert clv(m, h, horizon=horizon, rate=r) == pytest.approx(explicit, rel=1e-12)


def test_a_year_of_discounting_matches_the_annual_rate() -> None:
    # the last day of the year is discounted by exactly 1 / (1 + r)
    assert daily_discount(0.1) ** 365 == pytest.approx(1 / 1.1)
    # without churn, a year's value lies between the discounted and undiscounted margin sums
    value = clv(1.0, 0.0, horizon=365, rate=0.1)
    assert 365 / 1.1 < value < 365


def test_value_falls_with_hazard_and_discount_and_rises_with_horizon() -> None:
    base = clv(10.0, 0.001, horizon=365, rate=0.1)
    assert clv(10.0, 0.002, horizon=365, rate=0.1) < base
    assert clv(10.0, 0.001, horizon=365, rate=0.2) < base
    assert clv(10.0, 0.001, horizon=730, rate=0.1) > base


def test_negative_margin_gives_negative_value() -> None:
    assert clv(-5.0, 0.001, horizon=90, rate=0.0) < 0


def test_sensitivity_grid_covers_every_combination() -> None:
    m = np.array([10.0, 20.0])
    h = np.array([0.001, 0.002])
    points = sensitivity(m, h, rates=[0.0, 0.1], horizons=[90, 365])
    assert [(p.annual_rate, p.horizon_days) for p in points] == [
        (0.0, 90),
        (0.0, 365),
        (0.1, 90),
        (0.1, 365),
    ]
    first = points[0]
    expected = clv(10.0, 0.001, horizon=90, rate=0.0) + clv(20.0, 0.002, horizon=90, rate=0.0)
    assert first.total_micros == pytest.approx(expected)
    assert first.mean_micros == pytest.approx(expected / 2)
    # a longer horizon is worth more; a higher discount rate is worth less
    assert points[1].total_micros > points[0].total_micros > points[2].total_micros
    assert sensitivity(np.array([]), np.array([]), rates=[0.0], horizons=[30])[0].mean_micros == 0


def test_expected_days_and_window_probability() -> None:
    assert expected_days(np.array([0.0]), 28)[0] == pytest.approx(28.0)
    assert expected_days(np.array([math.log(2.0)]), 2)[0] == pytest.approx(0.75)
    p = window_probability(np.array([0.001]), 28)[0]
    assert p == pytest.approx(1 - math.exp(-0.028))
    assert hazard_from_window(float(p), 28) == pytest.approx(0.001)


@pytest.mark.parametrize(
    "call",
    [
        lambda: daily_discount(-0.1),
        lambda: daily_discount(1.0),
        lambda: lifetime_value(np.ones(1), np.ones(1), horizon_days=0, annual_rate=0.1),
        lambda: lifetime_value(np.ones(1), -np.ones(1), horizon_days=10, annual_rate=0.1),
        lambda: lifetime_value(np.ones(1), np.array([np.nan]), horizon_days=10, annual_rate=0.1),
        lambda: hazard_from_window(1.0, 28),
    ],
)
def test_invalid_inputs_are_refused(call: object) -> None:
    with pytest.raises(ValueError):
        call()  # type: ignore[operator]
