"""Independent, deliberately naive reference for the optimiser (tests only).

Written from the specification in ``docs/pricing.md`` with plain Python loops, sharing no code
with ``praxis.pricing.objective`` / ``constraints``: one price at a time, every constraint
checked on the exact integer price. Used for brute-force golden cases and as the oracle of the
property tests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

from praxis.pricing.config import PricingPolicy
from praxis.pricing.problem import PricingProblem

TOL = 1e-9


def zs(m: int) -> list[float]:
    nd = NormalDist()
    return [nd.inv_cdf((j + 0.5) / m) for j in range(m)]


CHURN_POINTS = 21  # the churn-slope grid of the specification


def contribution_draws(problem: PricingProblem, price: int, z: list[float]) -> list[float]:
    """Contribution per day at ``price`` for each elasticity shock z."""
    x = math.log(price / problem.current_price_micros)
    net = price * (1.0 - problem.payment_loss_rate)
    out = []
    for zz in z:
        total = 0.0
        for s in problem.segments:
            e = problem.elasticity[s.tier].mean + problem.elasticity[s.tier].sd * zz
            demand = s.demand * math.exp(e * x)
            total += demand * s.served_share * (net - problem.regions[s.region].unit_cost_micros)
        out.append(total)
    return out


@dataclass(frozen=True)
class Scored:
    price: int
    risk_adjusted: float
    prob_improvement: float
    delta_mean: float


def score(problem: PricingProblem, policy: PricingPolicy, price: int, z: list[float]) -> Scored:
    """Objective change vs the current price over every (elasticity, churn-slope) shock."""
    churn = problem.churn
    x = math.log(price / problem.current_price_micros)
    per_slope = churn.exposed_customers * churn.clv_micros * x / churn.window_days
    base = contribution_draws(problem, problem.current_price_micros, z)
    cand = contribution_draws(problem, price, z)
    delta = [
        (c - b) - per_slope * (churn.slope + churn.slope_sd * u)
        for c, b in zip(cand, base, strict=True)
        for u in zs(CHURN_POINTS)
    ]
    mean = sum(delta) / len(delta)
    sd = math.sqrt(sum((d - mean) ** 2 for d in delta) / len(delta))
    return Scored(
        price,
        mean - policy.uncertainty.risk_aversion * sd,
        sum(d > 0 for d in delta) / len(delta),
        mean,
    )


def violated(
    problem: PricingProblem, policy: PricingPolicy, price: int, z: list[float]
) -> set[str]:
    """Names of every constraint the integer ``price`` breaks."""
    out: set[str] = set()
    p0 = problem.current_price_micros
    bounds = policy.products[problem.product]
    c = policy.constraints
    if price < bounds.floor_micros:
        out.add("price_floor")
    if price > bounds.ceiling_micros:
        out.add("price_ceiling")
    if not (p0 / (1 + c.max_step) * (1 - TOL) <= price <= p0 * (1 + c.max_step) * (1 + TOL)):
        out.add("max_step")
    if _in_cooldown(problem, policy) and price != p0:
        out.add("cooldown")
    anchor = problem.anchor_price_micros
    if anchor is None or abs(math.log(price / anchor)) > policy.evidence.max_extrapolation + TOL:
        out.add("extrapolation_limit")
    if _margin_broken(problem, policy, price):
        out.add("margin_floor")
    if _capacity_broken(problem, policy, price, z):
        out.add("capacity")
    x = math.log(price / p0)
    if max(problem.churn.slope_upper, 0.0) * max(x, 0.0) > c.max_incremental_churn + TOL:
        out.add("churn_guardrail")
    return out


def _margin_broken(problem: PricingProblem, policy: PricingPolicy, price: int) -> bool:
    net = price * (1 - problem.payment_loss_rate)
    floor = policy.constraints.min_contribution_margin - TOL
    return any(
        (net - problem.regions[s.region].unit_cost_micros) / net < floor for s in problem.segments
    )


def _in_cooldown(problem: PricingProblem, policy: PricingPolicy) -> bool:
    last = problem.last_change
    return last is not None and (problem.as_of - last).days < policy.constraints.cooldown_days


def _capacity_broken(
    problem: PricingProblem, policy: PricingPolicy, price: int, z: list[float]
) -> bool:
    x = math.log(price / problem.current_price_micros)
    for region in {s.region for s in problem.segments}:
        segs = [s for s in problem.segments if s.region == region]
        base = sum(s.demand_high for s in segs)
        if base <= 0:
            continue
        worst = max(
            sum(
                s.demand_high
                * math.exp(
                    (problem.elasticity[s.tier].mean + problem.elasticity[s.tier].sd * zz) * x
                )
                for s in segs
            )
            / base
            for zz in z
        )
        util = problem.regions[region].utilization
        if worst > 1 + TOL and util * worst > policy.constraints.max_utilization + TOL:
            return True
    return False


def brute_force(problem: PricingProblem, policy: PricingPolicy) -> Scored | None:
    """Best feasible tick price in the step range (or the current price), by exhaustion."""
    z = zs(policy.uncertainty.z_grid)
    tick = policy.policy.price_tick_micros
    p0 = problem.current_price_micros
    c = policy.constraints
    lo = math.ceil(p0 / (1 + c.max_step) / tick)
    hi = math.floor(p0 * (1 + c.max_step) / tick)
    prices = sorted({k * tick for k in range(max(lo, 1), hi + 1)} | {p0})
    feasible = [p for p in prices if not violated(problem, policy, p, z)]
    if not feasible:
        return None
    return max((score(problem, policy, p, z) for p in feasible), key=lambda s: s.risk_adjusted)
