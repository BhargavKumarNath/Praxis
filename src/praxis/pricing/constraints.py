"""Hard constraints, checked for every candidate price (True = satisfied).

Every constraint is evaluated pointwise on the exact integer price that would be charged, and
the optimiser only ever returns a price for which all of them hold. Names double as reason
codes on decisions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
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
    # long-horizon guardrails (Phase 9, ADR 0016)
    SEGMENT_CHURN = "segment_churn_guardrail"
    CHURN_CEILING = "churn_risk_ceiling"
    CHANGE_FREQUENCY = "price_change_frequency"


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
    seg = segment_projection(problem, policy, x)
    out[Constraint.SEGMENT_CHURN] = np.all(seg.incremental <= seg.limits[None, :] + EPS, axis=1)
    below = seg.window <= policy.long_term.max_segment_window_churn + EPS
    out[Constraint.CHURN_CEILING] = (x <= EPS) | np.all(below, axis=1)
    over = customers_over_change_limit(problem, policy)
    out[Constraint.CHANGE_FREQUENCY] = (p == p0) | (over == 0)
    return out


def projected_churn(problem: PricingProblem, log_ratio: NDArray[np.float64]) -> NDArray[np.float64]:
    """Extra voluntary churn probability per exposed customer over the window (upper bound).

    Raises only: a cut is never credited with lower churn here (that would let noise in the
    churn estimate justify cuts); the objective values the posterior symmetrically.
    """
    c = problem.churn
    exposed = c.exposed_customers
    if exposed <= 0:
        return np.zeros(len(log_ratio))
    upper = max(c.response_upper, 0.0)
    projected: NDArray[np.float64] = upper * c.total_exposure * np.maximum(log_ratio, 0.0) / exposed
    return projected


@dataclass(frozen=True)
class SegmentProjection:
    """Per judged segment (enough customers): projected churn at the response upper bound."""

    names: tuple[str, ...]
    limits: NDArray[np.float64]  # (G,) incremental churn limit per segment
    incremental: NDArray[np.float64]  # (k, G) extra churn probability per exposed customer
    window: NDArray[np.float64]  # (k, G) baseline + extra voluntary churn probability


def segment_projection(
    problem: PricingProblem, policy: PricingPolicy, log_ratio: NDArray[np.float64]
) -> SegmentProjection:
    lt = policy.long_term
    judged = [g for g in problem.churn.segments if g.exposed_customers >= lt.min_segment_customers]
    upper = max(problem.churn.response_upper, 0.0)
    rise = np.maximum(log_ratio, 0.0)[:, None]
    exposed = np.array([g.exposed_customers for g in judged])
    inc = upper * rise * np.array([g.churn_exposure for g in judged])[None, :] / exposed
    base = np.array([g.window_churn for g in judged]) / exposed
    limits = np.array(
        [
            lt.max_segment_incremental_churn.get(
                g.segment, policy.constraints.max_incremental_churn
            )
            for g in judged
        ]
    )
    shape = (len(log_ratio), len(judged))
    return SegmentProjection(
        tuple(g.segment for g in judged),
        limits,
        inc.reshape(shape),
        (base[None, :] + inc).reshape(shape),
    )


def customers_over_change_limit(problem: PricingProblem, policy: PricingPolicy) -> int:
    """Exposed customers whom one more price change would put over the frequency limit."""
    limit = policy.long_term.max_price_changes_per_customer
    return sum(n for k, n in problem.price_change_counts.items() if k + 1 > limit)


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
