"""Constrained pricing optimiser: one product, one cycle -> one auditable ``Decision``.

Order of operations (each step can end the decision safely, with a reason code):

1. impossible inputs (``PricingProblem.validate``)                     -> UNAVAILABLE
2. evidence and uncertainty gates (tested product, evidence age, plausible and precise
   elasticity for every material tier, forecast width)                 -> FROZEN
3. candidates: a symmetric log grid over the max-step range, on the price tick, plus the
   current price; objective terms per candidate (``objective.evaluate``)
4. non-finite objective                                                 -> UNAVAILABLE
5. hard constraints per candidate (``constraints.check``); none feasible -> INFEASIBLE
6. best feasible candidate by risk-adjusted objective, refined over every tick between its
   grid neighbours (re-checked: a returned price always satisfies every constraint)
7. no improvement on the current price (or cooldown)                    -> HOLD
8. P(objective improves) below the policy minimum                       -> FROZEN
9. otherwise                                                            -> CHANGE

The optimiser is pure and deterministic: no clock, no randomness, no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
from numpy.typing import NDArray

from praxis.pricing import constraints as cons
from praxis.pricing.config import Mode, PricingPolicy
from praxis.pricing.decision import Candidate, Decision, Reason, Status, cycle_id
from praxis.pricing.objective import Evaluation, evaluate, z_grid
from praxis.pricing.problem import Blocked, PricingProblem

I64 = NDArray[np.int64]
MAX_REFINE_TICKS = 400


@dataclass(frozen=True)
class _View:
    """Evaluated candidates, the candidate the record focuses on, and whether it is chosen."""

    ev: Evaluation
    mask: dict[cons.Constraint, NDArray[np.bool_]]
    focus: int | None = None
    chosen: bool = False


def optimise(problem: PricingProblem, policy: PricingPolicy, mode: Mode | None = None) -> Decision:
    head = _Head(problem, policy, mode or policy.policy.mode)
    outcome = _solve(problem, policy)
    return head.decide(outcome.status, outcome.reasons, outcome.view, outcome.errors)


@dataclass(frozen=True)
class _Outcome:
    status: Status
    reasons: tuple[str, ...]
    view: _View | None = None
    errors: tuple[str, ...] = ()


def _solve(problem: PricingProblem, policy: PricingPolicy) -> _Outcome:
    errors = problem.validate()
    if problem.product not in policy.products:
        errors.append(f"no price bounds for product {problem.product} in the policy")
    if errors:
        return _Outcome(Status.UNAVAILABLE, (Reason.INVALID_INPUT,), errors=tuple(errors))
    gate = evidence_gate(problem, policy)
    if gate:
        return _Outcome(Status.FROZEN, gate)

    prices = candidate_prices(problem, policy)
    z = z_grid(policy.uncertainty.z_grid)
    ev = evaluate(problem, prices, z=z, risk_aversion=policy.uncertainty.risk_aversion)
    if not _finite(ev):
        return _Outcome(Status.UNAVAILABLE, (Reason.INVALID_OBJECTIVE,))
    mask = cons.check(problem, policy, ev)
    ok = cons.feasible(mask)
    if not ok.any():
        return _Outcome(Status.INFEASIBLE, (Reason.INFEASIBLE,), _View(ev, mask))
    best = int(np.flatnonzero(ok)[np.argmax(ev.risk_adjusted[ok])])
    return _choose(problem, policy, _refine(problem, policy, z, ev, mask, best))


def _choose(problem: PricingProblem, policy: PricingPolicy, view: _View) -> _Outcome:
    ev, mask = view.ev, view.mask
    current = _index(ev, problem.current_price_micros)
    best = current if view.focus is None else view.focus
    current_ok = bool(cons.feasible(mask)[current])
    if current_ok and (best == current or ev.risk_adjusted[best] <= ev.risk_adjusted[current]):
        reason = Reason.COOLDOWN if cons.in_cooldown(problem, policy) else Reason.NO_IMPROVEMENT
        return _Outcome(Status.HOLD, (reason,), _View(ev, mask, current))
    # A move away from an infeasible current price restores the hard constraints; it is not
    # gated on confidence. Any other move must be likely to improve the objective.
    forced = not current_ok
    if not forced and ev.prob_improvement[best] < policy.uncertainty.min_prob_improvement:
        return _Outcome(Status.FROZEN, (Reason.UNCERTAINTY_LOW_CONFIDENCE,), _View(ev, mask, best))
    reasons = _binding(ev, mask, best, current)
    if forced:
        reasons = (Reason.CURRENT_PRICE_INFEASIBLE.value, *reasons)
    return _Outcome(Status.CHANGE, reasons, _View(ev, mask, best, chosen=True))


# --------------------------------------------------------------------------- gates
def evidence_gate(problem: PricingProblem, policy: PricingPolicy) -> tuple[str, ...]:
    """Reasons the price must not move at all, before any objective is computed."""
    if problem.anchor_price_micros is None or problem.evidence_date is None:
        return (Reason.INSUFFICIENT_EVIDENCE,)
    if (problem.as_of - problem.evidence_date).days > policy.evidence.max_evidence_age_days:
        return (Reason.EVIDENCE_TOO_OLD,)
    reasons: list[str] = []
    shares = problem.tier_shares()
    material = [t for t, s in shares.items() if s > policy.uncertainty.material_tier_share]
    if any(problem.elasticity[t].mean >= 0 for t in material):
        reasons.append(Reason.IMPLAUSIBLE_ELASTICITY)
    if any(problem.elasticity[t].sd > policy.uncertainty.max_elasticity_sd for t in material):
        reasons.append(Reason.UNCERTAINTY_ELASTICITY_SD)
    if problem.forecast_relative_width > policy.uncertainty.max_forecast_relative_width:
        reasons.append(Reason.UNCERTAINTY_FORECAST_WIDTH)
    return tuple(reasons)


# ----------------------------------------------------------------------- candidates
def candidate_prices(problem: PricingProblem, policy: PricingPolicy) -> I64:
    """Symmetric log grid over the max-step range, on the tick, plus the current price."""
    p0 = problem.current_price_micros
    tick = policy.policy.price_tick_micros
    lo, hi = cons.step_bounds(p0, policy.constraints.max_step, tick)
    if lo > hi:
        return np.array([p0], dtype=np.int64)
    n = policy.policy.grid_points
    grid = np.exp(np.linspace(math.log(lo), math.log(hi), n))
    ticks = np.clip(np.rint(grid / tick).astype(np.int64) * tick, lo, hi)
    return np.unique(np.append(ticks, p0)).astype(np.int64)


def _refine(
    problem: PricingProblem,
    policy: PricingPolicy,
    z: NDArray[np.float64],
    ev: Evaluation,
    mask: dict[cons.Constraint, NDArray[np.bool_]],
    best: int,
) -> _View:
    """Search the ticks between the best grid candidate's neighbours, then polish.

    Stage 1 evaluates up to ``MAX_REFINE_TICKS`` evenly spaced ticks between the neighbours;
    stage 2 evaluates EVERY tick within one stage-1 spacing of the stage-1 best, so an optimum
    on a constraint boundary is found to the exact tick.
    """
    tick = policy.policy.price_tick_micros
    prices = ev.prices
    left = math.ceil(int(prices[max(best - 1, 0)]) / tick)
    right = math.floor(int(prices[min(best + 1, len(prices) - 1)]) / tick)
    view = _add_ticks(problem, policy, z, _View(ev, mask, best), left, right)
    spacing = math.ceil((right - left + 1) / MAX_REFINE_TICKS)
    if spacing <= 1:
        return view
    centre = int(view.ev.prices[_focus(view)]) // tick
    return _add_ticks(
        problem,
        policy,
        z,
        view,
        max(left, centre - spacing - 1),
        min(right, centre + spacing + 1),
    )


def _add_ticks(
    problem: PricingProblem,
    policy: PricingPolicy,
    z: NDArray[np.float64],
    view: _View,
    first: int,
    last: int,
) -> _View:
    """Re-evaluate with tick prices ``first..last`` (at most MAX_REFINE_TICKS of them) added."""
    tick = policy.policy.price_tick_micros
    if last - first + 1 > MAX_REFINE_TICKS:
        ticks = np.unique(np.linspace(first, last, MAX_REFINE_TICKS).round().astype(np.int64))
    else:
        ticks = np.arange(first, last + 1, dtype=np.int64)
    extra = np.setdiff1d(ticks * tick, view.ev.prices)
    extra = extra[extra > 0]
    if extra.size == 0:
        return view
    merged = np.unique(np.concatenate([view.ev.prices, extra])).astype(np.int64)
    ev = evaluate(problem, merged, z=z, risk_aversion=policy.uncertainty.risk_aversion)
    if not _finite(ev):
        return view
    mask = cons.check(problem, policy, ev)
    ok = cons.feasible(mask)
    return _View(ev, mask, int(np.flatnonzero(ok)[np.argmax(ev.risk_adjusted[ok])]))


def _focus(view: _View) -> int:
    return 0 if view.focus is None else view.focus


def _binding(
    ev: Evaluation, mask: dict[cons.Constraint, NDArray[np.bool_]], best: int, current: int
) -> tuple[str, ...]:
    """Constraints that stop the price moving further in the chosen direction."""
    step = 1 if best > current else -1
    nxt = best + step
    if nxt < 0 or nxt >= len(ev.prices):
        return (cons.Constraint.MAX_STEP.value,)
    blocked = cons.violations(mask, nxt)
    return blocked if blocked else (Reason.OPTIMUM_INTERIOR.value,)


def _index(ev: Evaluation, price: int) -> int:
    return int(np.flatnonzero(ev.prices == price)[0])


def _finite(ev: Evaluation) -> bool:
    arrays = (
        ev.expected_demand,
        ev.contribution,
        ev.churn_cost,
        ev.delta_mean,
        ev.delta_sd,
        ev.risk_adjusted,
        ev.prob_improvement,
        ev.capacity_ratio,
        ev.net_margin,
    )
    return all(bool(np.isfinite(a).all()) for a in arrays)


# ---------------------------------------------------------------------- the record
class _Head:
    """Builds decisions that share the problem, policy and mode."""

    def __init__(self, problem: PricingProblem, policy: PricingPolicy, mode: Mode) -> None:
        self.problem = problem
        self.policy = policy
        self.mode = mode

    def decide(
        self,
        status: Status,
        reasons: tuple[str, ...],
        view: _View | None = None,
        errors: tuple[str, ...] = (),
    ) -> Decision:
        p = self.problem
        candidates: tuple[Candidate, ...] = ()
        guardrails: dict[str, Any] = {}
        prediction = None
        chosen_price = None
        if view is not None:
            ev = view.ev
            candidates = _candidates(ev, view.mask)
            at = view.focus if view.focus is not None else _index(ev, p.current_price_micros)
            guardrails = _guardrails(p, self.policy, ev, view.mask, at)
            prediction = _prediction(ev, at)
            chosen_price = int(ev.prices[at]) if view.chosen else None
        return Decision(
            cycle_id=cycle_id(self.policy.policy.name, self.mode, p.as_of),
            product=p.product,
            as_of=p.as_of,
            mode=self.mode,
            policy_version=self.policy.version,
            status=status,
            current_price_micros=p.current_price_micros if p.current_price_micros > 0 else None,
            chosen_price_micros=chosen_price,
            reason_codes=tuple(str(r) for r in reasons),
            lineage=dict(p.lineage),
            inputs=_inputs(p) if not errors else {},
            constraints=_constraint_limits(p, self.policy),
            candidates=candidates,
            guardrails=guardrails,
            prediction=prediction,
            errors=errors,
        )


def _candidates(
    ev: Evaluation, mask: dict[cons.Constraint, NDArray[np.bool_]]
) -> tuple[Candidate, ...]:
    ok = cons.feasible(mask)
    return tuple(
        Candidate(
            price_micros=int(ev.prices[i]),
            log_ratio=float(ev.log_ratio[i]),
            expected_demand=float(ev.expected_demand[i]),
            contribution=float(ev.contribution[i]),
            churn_cost=float(ev.churn_cost[i]),
            delta_mean=float(ev.delta_mean[i]),
            delta_sd=float(ev.delta_sd[i]),
            delta_p05=float(ev.delta_p05[i]),
            delta_p95=float(ev.delta_p95[i]),
            delta_contribution_mean=float(ev.delta_contribution_mean[i]),
            prob_improvement=float(ev.prob_improvement[i]),
            risk_adjusted=float(ev.risk_adjusted[i]),
            feasible=bool(ok[i]),
            violations=cons.violations(mask, i),
        )
        for i in range(len(ev.prices))
    )


def _guardrails(
    p: PricingProblem,
    policy: PricingPolicy,
    ev: Evaluation,
    mask: dict[cons.Constraint, NDArray[np.bool_]],
    at: int,
) -> dict[str, Any]:
    util = np.array([p.regions[r].utilization for r in ev.regions])
    projected_util = util * ev.capacity_ratio[at]
    checks = {name.value: bool(ok[at]) for name, ok in mask.items()}
    return {
        "price_micros": int(ev.prices[at]),
        "passed": all(checks.values()),
        "checks": checks,
        "projected_incremental_churn": float(cons.projected_churn(p, ev.log_ratio[at : at + 1])[0]),
        "projected_max_utilization": float(projected_util.max()) if util.size else 0.0,
        "min_net_margin": float(ev.net_margin[at].min()) if ev.net_margin.size else 0.0,
        "extrapolation": None
        if p.anchor_price_micros is None
        else abs(math.log(int(ev.prices[at]) / p.anchor_price_micros)),
    }


def _prediction(ev: Evaluation, at: int) -> dict[str, Any]:
    return {
        "price_micros": int(ev.prices[at]),
        "per_day": True,
        "delta_objective_mean": float(ev.delta_mean[at]),
        "delta_objective_sd": float(ev.delta_sd[at]),
        "delta_objective_p05": float(ev.delta_p05[at]),
        "delta_objective_p95": float(ev.delta_p95[at]),
        "delta_contribution_mean": float(ev.delta_contribution_mean[at]),
        "churn_cost": float(ev.churn_cost[at]),
        "prob_improvement": float(ev.prob_improvement[at]),
        "expected_demand": float(ev.expected_demand[at]),
    }


def _inputs(p: PricingProblem) -> dict[str, Any]:
    return {
        "segments": [
            {
                "region": s.region,
                "tier": s.tier,
                "demand": s.demand,
                "demand_high": s.demand_high,
                "served_share": s.served_share,
            }
            for s in p.segments
        ],
        "regions": {
            r: {"unit_cost_micros": c.unit_cost_micros, "utilization": c.utilization}
            for r, c in sorted(p.regions.items())
        },
        "elasticity": {t: {"mean": e.mean, "sd": e.sd} for t, e in sorted(p.elasticity.items())},
        "payment_loss_rate": p.payment_loss_rate,
        "churn": {
            "slope": p.churn.slope,
            "slope_upper": p.churn.slope_upper,
            "slope_sd": p.churn.slope_sd,
            "relative_slope": p.churn.relative_slope,
            "base_window_churn": p.churn.base_window_churn,
            "window_days": p.churn.window_days,
            "exposed_customers": p.churn.exposed_customers,
            "clv_micros": p.churn.clv_micros,
        },
        "forecast_relative_width": p.forecast_relative_width,
        "anchor_price_micros": p.anchor_price_micros,
        "evidence_date": None if p.evidence_date is None else p.evidence_date.isoformat(),
        "last_change": None if p.last_change is None else p.last_change.isoformat(),
    }


def _constraint_limits(p: PricingProblem, policy: PricingPolicy) -> dict[str, Any]:
    bounds = policy.products.get(p.product)
    c = policy.constraints
    tick = policy.policy.price_tick_micros
    step = (
        cons.step_bounds(p.current_price_micros, c.max_step, tick)
        if p.current_price_micros > 0
        else None
    )
    return {
        "price_floor_micros": None if bounds is None else bounds.floor_micros,
        "price_ceiling_micros": None if bounds is None else bounds.ceiling_micros,
        "max_step": c.max_step,
        "step_range_micros": None if step is None else list(step),
        "cooldown_days": c.cooldown_days,
        "in_cooldown": cons.in_cooldown(p, policy),
        "max_extrapolation": policy.evidence.max_extrapolation,
        "min_contribution_margin": c.min_contribution_margin,
        "max_utilization": c.max_utilization,
        "max_incremental_churn": c.max_incremental_churn,
        "min_prob_improvement": policy.uncertainty.min_prob_improvement,
        "max_elasticity_sd": policy.uncertainty.max_elasticity_sd,
        "max_forecast_relative_width": policy.uncertainty.max_forecast_relative_width,
        "price_tick_micros": tick,
    }


def unavailable(blocked: Blocked, as_of: date, policy: PricingPolicy, mode: Mode) -> Decision:
    """The decision recorded when no problem could even be built (inputs missing / stale)."""
    return Decision(
        cycle_id=cycle_id(policy.policy.name, mode, as_of),
        product=blocked.product,
        as_of=as_of,
        mode=mode,
        policy_version=policy.version,
        status=Status.UNAVAILABLE,
        current_price_micros=blocked.current_price_micros,
        chosen_price_micros=None,
        reason_codes=(blocked.reason,),
        lineage=dict(blocked.lineage),
        errors=(blocked.detail,),
    )
