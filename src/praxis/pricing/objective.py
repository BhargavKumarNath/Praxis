"""Expected objective of candidate prices, with elasticity and churn-response uncertainty.

Demand of segment s (region r, tier t) at price p, relative to the current price p0:

    D_s(p) = B_s * (p / p0) ** e_t        (constant elasticity inside the tested range)

B_s is the forecast at the current price; e_t the tier's causal elasticity (Phase 5). The
forecast's own price features are predictive, not causal, so they never price a change
(ADR 0013). Per day, in micro-GBP:

    contribution(p) = sum_s D_s(p) * served_s * (p * (1 - payment_loss) - cost_r)
    churn_cost(p)   = exposed * CLV * churn_slope * log(p / p0) / window
    objective(p)    = contribution(p) - churn_cost(p)

Uncertainty: e_t = mean_t + sd_t * z with ONE shock z shared by all tiers (comonotone; the
artifact holds marginals only, and a common shock gives the widest spread of the total), and
an independent shock on the churn slope, slope = mean + sd * u (its posterior; ADR 0013
amendment: the churn response is the least precisely measured input, so leaving its
uncertainty out lets noise drive decisions). z and u run over equal-probability grids, so
expectations, SDs, quantiles and P(improvement) are deterministic. ``delta`` is the objective
minus the current price's, per (z, u).
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import NormalDist

import numpy as np
from numpy.typing import NDArray

from praxis.pricing.problem import PricingProblem

F64 = NDArray[np.float64]
I64 = NDArray[np.int64]


CHURN_GRID = 21  # churn-slope shock points (the objective is linear in the slope)


def z_grid(m: int) -> F64:
    """Equal-probability standard-normal points (midpoint quantiles)."""
    nd = NormalDist()
    return np.array([nd.inv_cdf((j + 0.5) / m) for j in range(m)], dtype=np.float64)


@dataclass(frozen=True)
class Evaluation:
    """Objective terms for k candidate prices (arrays of shape (k,) unless noted)."""

    prices: I64
    log_ratio: F64
    expected_demand: F64  # requested units per day, mean over z
    contribution: F64  # micro-GBP per day, mean over z
    churn_cost: F64  # micro-GBP per day
    delta_mean: F64  # objective change vs the current price
    delta_sd: F64
    delta_p05: F64
    delta_p95: F64
    delta_contribution_mean: F64  # contribution change only (no churn valuation)
    prob_improvement: F64  # P(delta > 0)
    risk_adjusted: F64  # delta_mean - risk_aversion * delta_sd
    regions: tuple[str, ...]
    capacity_ratio: F64  # (k, R) worst-case demand ratio vs current, at the upper quantile
    net_margin: F64  # (k, R) (net price - cost) / net price


def evaluate(problem: PricingProblem, prices: I64, *, z: F64, risk_aversion: float) -> Evaluation:
    """``prices`` must contain the current price (its delta is 0 by construction).

    Overflow is not an error here: the optimiser rejects any non-finite result explicitly.
    """
    with np.errstate(over="ignore", invalid="ignore"):
        return _evaluate(problem, prices, z, risk_aversion)


def _evaluate(problem: PricingProblem, prices: I64, z: F64, risk_aversion: float) -> Evaluation:
    p0 = float(problem.current_price_micros)
    p = prices.astype(np.float64)
    x = np.log(p / p0)  # (k,)
    seg = problem.segments
    mean = np.array([problem.elasticity[s.tier].mean for s in seg])
    sd = np.array([problem.elasticity[s.tier].sd for s in seg])
    e = mean[:, None] + sd[:, None] * z[None, :]  # (S, m)
    ratio = np.exp(x[:, None, None] * e[None, :, :])  # (k, S, m)

    base = np.array([s.demand for s in seg])
    served = np.array([s.served_share for s in seg])
    cost = np.array([problem.regions[s.region].unit_cost_micros for s in seg])
    net_price = p * (1.0 - problem.payment_loss_rate)  # (k,)
    unit_margin = net_price[:, None] - cost[None, :]  # (k, S)
    demand = base[None, :, None] * ratio  # (k, S, m)
    contrib = np.einsum("ksm,s,ks->km", demand, served, unit_margin)

    c = problem.churn
    per_unit_slope = c.exposed_customers * c.clv_micros * x / c.window_days  # (k,)
    churn_cost = per_unit_slope * c.slope
    current = int(np.flatnonzero(prices == problem.current_price_micros)[0])
    delta_contrib = contrib - contrib[current][None, :]  # (k, m)
    slopes = c.slope + c.slope_sd * z_grid(CHURN_GRID)  # (u,)
    delta = (
        delta_contrib[:, :, None] - per_unit_slope[:, None, None] * slopes[None, None, :]
    ).reshape(len(prices), -1)  # (k, m * u); exactly 0 at the current price
    delta_mean = delta.mean(axis=1)
    delta_sd = delta.std(axis=1)

    regions = tuple(sorted({s.region for s in seg}))
    high = np.array([s.demand_high for s in seg])
    cap = np.empty((len(prices), len(regions)))
    for j, r in enumerate(regions):
        sel = np.array([s.region == r for s in seg])
        w = high[sel]
        total = w.sum()
        if total > 0:
            cap[:, j] = (np.einsum("s,ksm->km", w, ratio[:, sel, :]) / total).max(axis=1)
        else:
            cap[:, j] = 1.0  # no demand in the region: the price cannot add load
    region_cost = np.array([problem.regions[r].unit_cost_micros for r in regions])
    net_margin = (net_price[:, None] - region_cost[None, :]) / net_price[:, None]

    return Evaluation(
        prices=prices,
        log_ratio=x,
        expected_demand=demand.sum(axis=1).mean(axis=1),
        contribution=contrib.mean(axis=1),
        churn_cost=churn_cost,
        delta_mean=delta_mean,
        delta_sd=delta_sd,
        delta_p05=np.quantile(delta, 0.05, axis=1),
        delta_p95=np.quantile(delta, 0.95, axis=1),
        delta_contribution_mean=delta_contrib.mean(axis=1),
        prob_improvement=(delta > 0).mean(axis=1),
        risk_adjusted=delta_mean - risk_aversion * delta_sd,
        regions=regions,
        capacity_ratio=cap,
        net_margin=net_margin,
    )
