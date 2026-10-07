"""Hard constraints, checked for every candidate price (True = satisfied).

Every constraint is evaluated pointwise on the exact integer price that would be charged, and
the optimiser only ever returns a price for which all of them hold. Names double as reason
codes on decisions.
"""

from __future__ import annotations

import math
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray

from praxis.pricing.config import PricingPolicy
from praxis.pricing.objective import Evaluation
from praxis.pricing.problem import PricingProblem

BOOL = NDArray[np.bool_]
# Relative slack for float comparisons on exact integer prices (far below one micro-GBP).
EPS = 1e-12


class Constraint(StrEnum):
    PRICE_FLOOR = "price_floor"
    PRICE_CEILING = "price_ceiling"
    MAX_STEP = "max_step"
    COOLDOWN = "cooldown"
    EXTRAPOLATION = "extrapolation_limit"
    MARGIN_FLOOR = "margin_floor"
    CAPACITY = "capacity"
    CHURN_GUARDRAIL = "churn_guardrail"


def in_cooldown(problem: PricingProblem, policy: PricingPolicy) -> bool:
    if problem.last_change is None:
        return False
    return (problem.as_of - problem.last_change).days < policy.constraints.cooldown_days


def check(problem: PricingProblem, policy: PricingPolicy, ev: Evaluation) -> dict[Constraint, BOOL]:
    """Satisfaction mask per constraint for the candidates in ``ev``."""
    bounds = policy.products[problem.product]
    c = policy.constraints
    p = ev.prices
    p0 = problem.current_price_micros
    x = ev.log_ratio
    out: dict[Constraint, BOOL] = {
        Constraint.PRICE_FLOOR: p >= bounds.floor_micros,
        Constraint.PRICE_CEILING: p <= bounds.ceiling_micros,
        Constraint.MAX_STEP: np.abs(x) <= policy.max_log_step + EPS,
        Constraint.COOLDOWN: (p == p0) | (not in_cooldown(problem, policy)),
    }
    anchor = problem.anchor_price_micros
    if anchor is None:
        out[Constraint.EXTRAPOLATION] = np.zeros(len(p), dtype=bool)
    else:
        drift = np.abs(np.log(p / anchor))
        out[Constraint.EXTRAPOLATION] = drift <= policy.evidence.max_extrapolation + EPS
    out[Constraint.MARGIN_FLOOR] = np.all(ev.net_margin >= c.min_contribution_margin - EPS, axis=1)
    util = np.array([problem.regions[r].utilization for r in ev.regions])
    projected = util[None, :] * ev.capacity_ratio
    ok_region = (ev.capacity_ratio <= 1.0 + EPS) | (projected <= c.max_utilization + EPS)
    out[Constraint.CAPACITY] = np.all(ok_region, axis=1)
    churn = projected_churn(problem, x)
    out[Constraint.CHURN_GUARDRAIL] = churn <= c.max_incremental_churn + EPS
    return out


def projected_churn(problem: PricingProblem, log_ratio: NDArray[np.float64]) -> NDArray[np.float64]:
    """Extra churn probability per exposed customer over the evidence window (upper bound).

    Raises only: a cut is never credited with lower churn here (that would let noise in the
    churn estimate justify cuts); the objective values the posterior symmetrically.
    """
    upper = max(problem.churn.slope_upper, 0.0)
    projected: NDArray[np.float64] = upper * np.maximum(log_ratio, 0.0)
    return projected


def violations(mask: dict[Constraint, BOOL], i: int) -> tuple[str, ...]:
    return tuple(name.value for name, ok in mask.items() if not ok[i])


def feasible(mask: dict[Constraint, BOOL]) -> BOOL:
    out = np.ones(len(next(iter(mask.values()))), dtype=bool)
    for ok in mask.values():
        out &= ok
    return out


def step_bounds(price: int, max_step: float, tick: int) -> tuple[int, int]:
    """Smallest and largest tick-aligned prices within one step of ``price``."""
    lo = math.ceil(price / (1.0 + max_step) / tick) * tick
    hi = math.floor(price * (1.0 + max_step) / tick) * tick
    return max(lo, tick), hi
