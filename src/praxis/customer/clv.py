"""Customer lifetime value (pure, closed form).

For a customer with daily contribution ``m`` and a constant daily churn hazard ``h`` (voluntary
plus involuntary), valued over ``H`` days at an annual discount rate ``r``:

    d      = (1 + r) ** (-1 / 365)              daily discount factor
    S(t)   = exp(-h t)                           survival to the end of day t
    CLV    = m * sum_{t=1..H} (d * exp(-h)) ** t = m * q (1 - q**H) / (1 - q),  q = d exp(-h)

``expected_days`` is the same sum without discounting or margin (expected active days within
the horizon). Day 1 is the first day after the valuation date: value already earned is sunk.
A negative margin gives a negative CLV (losing that customer is a gain); callers decide how
to aggregate.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

F64 = NDArray[np.float64]


def daily_discount(annual_rate: float) -> float:
    if not 0.0 <= annual_rate < 1.0:
        raise ValueError("annual discount rate must lie in [0, 1)")
    return float((1.0 + annual_rate) ** (-1.0 / 365.0))


def _geometric(q: F64, horizon: int) -> F64:
    """sum_{t=1..H} q**t, exact at q == 1 (no hazard, no discount)."""
    near_one = np.abs(1.0 - q) < 1e-12
    safe = np.where(near_one, 0.5, q)
    out: F64 = np.where(near_one, float(horizon), safe * (1.0 - safe**horizon) / (1.0 - safe))
    return out


def lifetime_value(
    margin_daily: F64, hazard_daily: F64, *, horizon_days: int, annual_rate: float
) -> F64:
    if horizon_days < 1:
        raise ValueError("horizon must be at least one day")
    if np.any(hazard_daily < 0) or not np.all(np.isfinite(hazard_daily)):
        raise ValueError("hazards must be finite and >= 0")
    q = daily_discount(annual_rate) * np.exp(-hazard_daily)
    out: F64 = margin_daily * _geometric(q, horizon_days)
    return out


def expected_days(hazard_daily: F64, horizon_days: int) -> F64:
    """Expected active days in the next ``horizon_days`` (no discounting)."""
    return _geometric(np.exp(-hazard_daily), horizon_days)


@dataclass(frozen=True)
class SensitivityPoint:
    annual_rate: float
    horizon_days: int
    total_micros: float
    mean_micros: float


def sensitivity(
    margin_daily: F64,
    hazard_daily: F64,
    *,
    rates: Sequence[float],
    horizons: Sequence[int],
) -> list[SensitivityPoint]:
    """Total and mean CLV under every (discount rate, horizon) combination."""
    out = []
    for r in rates:
        for h in horizons:
            v = lifetime_value(margin_daily, hazard_daily, horizon_days=h, annual_rate=r)
            n = max(len(v), 1)
            out.append(SensitivityPoint(float(r), int(h), float(v.sum()), float(v.sum()) / n))
    return out


def window_probability(hazard_daily: F64, window_days: int) -> F64:
    """P(churn within ``window_days``) under a constant daily hazard."""
    out: F64 = 1.0 - np.exp(-hazard_daily * window_days)
    return out


def hazard_from_window(prob: float, window_days: int) -> float:
    """Inverse of ``window_probability`` for one value."""
    if not 0.0 <= prob < 1.0:
        raise ValueError("window probability must lie in [0, 1)")
    return -math.log1p(-prob) / window_days
