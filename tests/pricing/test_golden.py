"""Golden cases (required_test.md s12): optima known in closed form or by exhaustion."""

from __future__ import annotations

import math
from datetime import date

import pytest

from praxis.pricing.decision import Status
from praxis.pricing.optimiser import optimise
from praxis.pricing.problem import ChurnResponse, RegionContext, SegmentBaseline, TierElasticity
from tests.pricing import reference
from tests.pricing.helpers import open_policy, policy, problem, single_segment


@pytest.mark.parametrize(
    ("elasticity", "cost", "start"),
    [(-2.0, 100_000.0, 180_000), (-3.0, 50_000.0, 90_000), (-1.5, 10_000.0, 25_000)],
)
def test_matches_the_closed_form_constant_elasticity_optimum(
    elasticity: float, cost: float, start: int
) -> None:
    """max (p - c) p^e  =>  p* = c e / (1 + e)  (Lerner rule), here inside the step range."""
    expected = cost * elasticity / (1.0 + elasticity)
    d = optimise(single_segment(elasticity, cost, start), open_policy())
    assert d.status is Status.CHANGE
    assert d.chosen_price_micros is not None
    assert d.chosen_price_micros == pytest.approx(expected, rel=2e-4)
    assert d.reason_codes == ("optimum_interior",)


def test_inelastic_demand_raises_to_the_step_limit() -> None:
    """|e| < 1: profit rises with price without bound, so the max-step constraint binds."""
    pol = open_policy(max_step=0.05)
    d = optimise(single_segment(-0.5, 10_000.0, 100_000), pol)
    assert d.status is Status.CHANGE
    assert d.chosen_price_micros == math.floor(100_000 * 1.05)
    assert d.reason_codes == ("max_step",)


def test_elastic_demand_far_above_the_optimum_cuts_to_the_step_limit() -> None:
    d = optimise(single_segment(-4.0, 10_000.0, 100_000), open_policy(max_step=0.05))
    assert d.status is Status.CHANGE
    assert d.chosen_price_micros == math.ceil(100_000 / 1.05)
    assert d.reason_codes == ("max_step",)


def test_at_the_optimum_the_price_holds() -> None:
    d = optimise(single_segment(-2.0, 100_000.0, 200_000), open_policy())
    assert d.status is Status.HOLD
    assert d.reason_codes == ("no_improvement",)
    assert d.chosen_price_micros is None


def test_churn_guardrail_caps_the_raise_exactly() -> None:
    slope_upper = 0.5
    pol = open_policy(max_step=0.05, max_incremental_churn=0.01)
    prob = single_segment(
        -0.5, 10_000.0, 100_000, churn=ChurnResponse(0.0, slope_upper, 28, 0.0, 0.0)
    )
    d = optimise(prob, pol)
    limit = 100_000 * math.exp(0.01 / slope_upper)  # largest price within the guardrail
    assert d.status is Status.CHANGE
    assert d.chosen_price_micros == math.floor(limit)
    assert d.reason_codes == ("churn_guardrail",)
    assert d.guardrails["projected_incremental_churn"] <= 0.01


def test_extrapolation_limit_binds_relative_to_the_tested_price() -> None:
    pol = open_policy(max_step=0.3)
    pol = pol.model_copy(
        update={"evidence": pol.evidence.model_copy(update={"max_extrapolation": 0.1})}
    )
    d = optimise(single_segment(-0.5, 10_000.0, 100_000, anchor_price_micros=95_000), pol)
    assert d.chosen_price_micros == math.floor(95_000 * math.exp(0.1))
    assert d.reason_codes == ("extrapolation_limit",)


def test_price_ceiling_binds() -> None:
    pol = policy(
        constraints={"max_step": 0.2, "cooldown_days": 0},
        products={"api_requests": {"floor_micros": 1, "ceiling_micros": 104_000}},
        policy={"price_tick_micros": 1},
        evidence={"max_extrapolation": 5.0},
    )
    d = optimise(single_segment(-0.5, 10_000.0, 100_000), pol)
    assert d.chosen_price_micros == 104_000
    assert d.reason_codes == ("price_ceiling",)


def test_capacity_blocks_a_profitable_cut_in_a_hot_region() -> None:
    """Elastic demand wants a cut; the region is near its utilisation limit."""
    prob = single_segment(
        -4.0, 10_000.0, 100_000, regions={"eu_west": RegionContext("eu_west", 10_000.0, 0.84)}
    )
    d = optimise(prob, open_policy(max_step=0.05, max_utilization=0.85))
    assert d.status is Status.CHANGE
    assert d.chosen_price_micros is not None and d.chosen_price_micros < 100_000
    assert d.reason_codes == ("capacity",)
    # demand ratio at the chosen price keeps utilisation at or under 0.85
    assert 0.84 * (d.chosen_price_micros / 100_000) ** -4.0 <= 0.85 + 1e-9
    assert d.guardrails["projected_max_utilization"] <= 0.85 + 1e-9


def test_margin_floor_forces_a_raise_after_a_cost_increase() -> None:
    """The current price violates the margin floor; the optimiser must restore it."""
    prob = single_segment(-1.5, 90_000.0, 100_000)
    d = optimise(prob, open_policy(max_step=0.2, min_contribution_margin=0.2))
    assert d.status is Status.CHANGE
    assert d.reason_codes[0] == "current_price_infeasible"
    assert d.chosen_price_micros is not None
    assert (d.chosen_price_micros - 90_000) / d.chosen_price_micros >= 0.2


def test_cooldown_holds_the_price() -> None:
    pol = open_policy(cooldown_days=14)
    d = optimise(single_segment(-0.5, 10_000.0, 100_000, last_change=date(2026, 7, 13)), pol)
    assert d.status is Status.HOLD
    assert d.reason_codes == ("cooldown",)
    assert d.constraints["in_cooldown"] is True


@pytest.mark.parametrize("seed", range(12))
def test_matches_brute_force_on_random_small_problems(seed: int) -> None:
    """Grid + local refinement finds the exhaustive optimum over every tick in the range."""
    import random

    rnd = random.Random(seed)  # noqa: S311 - test-case generation, not crypto
    tiers = ("starter", "growth", "enterprise")
    elasticity = {t: TierElasticity(-rnd.uniform(0.4, 3.0), rnd.uniform(0.0, 0.15)) for t in tiers}
    regions = {
        r: RegionContext(r, rnd.uniform(5e4, 3e5), rnd.uniform(0.3, 0.95)) for r in ("a", "b")
    }
    segments = tuple(
        SegmentBaseline(r, t, demand, demand * rnd.uniform(1.0, 1.4), rnd.uniform(0.9, 1.0))
        for r in regions
        for t in tiers
        if (demand := rnd.uniform(0.0, 5_000.0)) > 500
    ) or (SegmentBaseline("a", "growth", 1_000.0, 1_100.0, 1.0),)
    prob = problem(
        current_price_micros=rnd.randrange(300_000, 500_000, 7),
        anchor_price_micros=400_000,
        elasticity=elasticity,
        segments=segments,
        regions=regions,
        churn=ChurnResponse(rnd.uniform(0, 0.05), rnd.uniform(0.05, 0.4), 28, 500.0, 3e8),
    )
    pol = policy(
        policy={"price_tick_micros": 100},
        uncertainty={"z_grid": 21, "min_prob_improvement": 0.5},
        constraints={"cooldown_days": 0, "max_step": 0.05},
    )
    best = reference.brute_force(prob, pol)
    d = optimise(prob, pol)
    if best is None:
        assert d.status is Status.INFEASIBLE
        return
    z = reference.zs(pol.uncertainty.z_grid)
    if d.status is Status.CHANGE:
        assert d.chosen_price_micros is not None
        mine = reference.score(prob, pol, d.chosen_price_micros, z)
        assert not reference.violated(prob, pol, d.chosen_price_micros, z)
        assert mine.risk_adjusted == pytest.approx(best.risk_adjusted, rel=1e-6, abs=1.0)
    else:
        # holding (or freezing) is only right when the current price is the exhaustive optimum
        # or the best move is not confident enough
        assert d.status in (Status.HOLD, Status.FROZEN)
        if d.status is Status.HOLD:
            assert best.price == prob.current_price_micros or best.risk_adjusted <= 1.0
        else:
            assert best.prob_improvement < pol.uncertainty.min_prob_improvement
