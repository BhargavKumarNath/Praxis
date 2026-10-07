"""Property tests (required_test.md s12): for random inputs no constraint is ever violated.

The oracle is ``tests.pricing.reference``: an independent scalar implementation that checks
each constraint on the exact integer price the optimiser returns.
"""

from __future__ import annotations

import json
import math
from datetime import timedelta
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from praxis.pricing.config import PricingPolicy
from praxis.pricing.constraints import in_cooldown
from praxis.pricing.decision import Status
from praxis.pricing.optimiser import optimise
from praxis.pricing.problem import (
    ChurnResponse,
    PricingProblem,
    RegionContext,
    SegmentBaseline,
    TierElasticity,
)
from tests.pricing import reference
from tests.pricing.helpers import AS_OF, PRODUCT, policy, problem

TIERS = ("starter", "growth", "enterprise")
REGIONS = ("r1", "r2", "r3")
SETTINGS = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@st.composite
def cases(draw: st.DrawFn) -> tuple[PricingProblem, PricingPolicy]:
    tick = draw(st.sampled_from([1, 10, 100]))
    p0 = draw(st.integers(2_000, 2_000_000))
    floor = int(p0 * draw(st.floats(0.5, 1.02)))
    ceiling = max(floor + 1, int(p0 * draw(st.floats(0.98, 2.0))))
    pol = policy(
        policy={"price_tick_micros": tick, "grid_points": draw(st.sampled_from([3, 11, 41]))},
        constraints={
            "max_step": draw(st.floats(0.005, 0.3)),
            "cooldown_days": draw(st.integers(0, 30)),
            "min_contribution_margin": draw(st.floats(0.0, 0.6)),
            "max_utilization": draw(st.floats(0.5, 1.0)),
            "max_incremental_churn": draw(st.floats(0.0, 0.05)),
        },
        uncertainty={
            "z_grid": 21,
            "max_elasticity_sd": draw(st.floats(0.05, 0.5)),
            "min_prob_improvement": draw(st.floats(0.5, 0.99)),
            "risk_aversion": draw(st.floats(0.0, 2.0)),
        },
        evidence={"max_extrapolation": draw(st.floats(0.01, 0.4))},
        products={PRODUCT: {"floor_micros": floor, "ceiling_micros": ceiling}},
    )
    regions = {
        r: RegionContext(r, draw(st.floats(0.0, 1.5)) * p0, draw(st.floats(0.0, 1.2)))
        for r in REGIONS
    }
    segments = []
    for r in REGIONS:
        for t in TIERS:
            if draw(st.booleans()):
                d = draw(st.floats(0.0, 1e5))
                segments.append(
                    SegmentBaseline(r, t, d, d * draw(st.floats(1.0, 2.0)), draw(st.floats(0, 1)))
                )
    if not segments:
        segments.append(SegmentBaseline("r1", "growth", 100.0, 120.0, 1.0))
    slope = draw(st.floats(-0.2, 0.3))
    fields: dict[str, Any] = {
        "current_price_micros": p0,
        "last_change": AS_OF - timedelta(days=draw(st.integers(0, 60))),
        "anchor_price_micros": int(p0 * math.exp(draw(st.floats(-0.3, 0.3)))),
        "elasticity": {
            t: TierElasticity(draw(st.floats(-4.0, -0.2)), draw(st.floats(0.0, 0.4))) for t in TIERS
        },
        "segments": tuple(segments),
        "regions": regions,
        "payment_loss_rate": draw(st.floats(0.0, 0.3)),
        "churn": ChurnResponse(
            slope, slope + draw(st.floats(0.0, 0.3)), 28, draw(st.floats(0, 1e4)), 1e8
        ),
        "forecast_relative_width": draw(st.floats(0.0, 0.9)),
    }
    return problem(**fields), pol


@SETTINGS
@given(cases())
def test_a_returned_price_never_violates_any_constraint(
    case: tuple[PricingProblem, PricingPolicy],
) -> None:
    prob, pol = case
    d = optimise(prob, pol)
    json.dumps(d.record(), allow_nan=False)  # always a valid, finite audit record
    if d.status is not Status.CHANGE:
        assert d.chosen_price_micros is None
        return
    price = d.chosen_price_micros
    assert price is not None and price != prob.current_price_micros
    z = reference.zs(pol.uncertainty.z_grid)
    assert reference.violated(prob, pol, price, z) == set()
    assert price % pol.policy.price_tick_micros == 0
    # bounds, max change, cooldown and churn guardrail, restated explicitly
    bounds = pol.products[PRODUCT]
    assert bounds.floor_micros <= price <= bounds.ceiling_micros
    assert abs(math.log(price / prob.current_price_micros)) <= pol.max_log_step + 1e-12
    assert not in_cooldown(prob, pol)
    assert d.guardrails["passed"] is True


@SETTINGS
@given(cases())
def test_an_unforced_change_is_confident_and_improves(
    case: tuple[PricingProblem, PricingPolicy],
) -> None:
    prob, pol = case
    d = optimise(prob, pol)
    if d.status is not Status.CHANGE or d.reason_codes[0] == "current_price_infeasible":
        return
    assert d.chosen_price_micros is not None
    z = reference.zs(pol.uncertainty.z_grid)
    s = reference.score(prob, pol, d.chosen_price_micros, z)
    assert s.prob_improvement >= pol.uncertainty.min_prob_improvement
    assert s.risk_adjusted > -1e-6 * max(1.0, abs(s.delta_mean))


@SETTINGS
@given(cases())
def test_cooldown_and_uncertainty_freeze_are_respected(
    case: tuple[PricingProblem, PricingPolicy],
) -> None:
    prob, pol = case
    d = optimise(prob, pol)
    if in_cooldown(prob, pol):
        assert d.status is not Status.CHANGE
    shares = prob.tier_shares()
    too_uncertain = any(
        prob.elasticity[t].sd > pol.uncertainty.max_elasticity_sd
        for t, share in shares.items()
        if share > pol.uncertainty.material_tier_share
    )
    if too_uncertain:
        assert d.status is Status.FROZEN
        assert "uncertainty_elasticity_sd" in d.reason_codes


@SETTINGS
@given(cases())
def test_decisions_are_deterministic(case: tuple[PricingProblem, PricingPolicy]) -> None:
    prob, pol = case
    a, b = optimise(prob, pol), optimise(prob, pol)
    assert a.decision_id == b.decision_id
    assert a.record_sha256 == b.record_sha256
