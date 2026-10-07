"""Failure cases (required_test.md s12): every bad input ends in an explicit, safe decision.

Missing / stale forecasts and unavailable models or costs are produced by the input builder
(``tests/pricing/test_service.py``); here the optimiser itself must refuse impossible numbers.
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from praxis.pricing.decision import Status
from praxis.pricing.optimiser import optimise
from praxis.pricing.problem import ChurnResponse, RegionContext, SegmentBaseline, TierElasticity
from tests.pricing.helpers import open_policy, policy, problem, single_segment

NAN, INF = math.nan, math.inf


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"current_price_micros": 0}, "current price"),
        ({"current_price_micros": -5}, "current price"),
        ({"segments": ()}, "no forecast segments"),
        ({"anchor_price_micros": -1}, "anchor price"),
        ({"payment_loss_rate": 1.0}, "payment loss"),
        ({"payment_loss_rate": NAN}, "payment loss"),
        ({"forecast_relative_width": INF}, "relative width"),
        ({"segments": (SegmentBaseline("eu_west", "growth", -1.0, 1.0, 1.0),)}, "demand"),
        ({"segments": (SegmentBaseline("eu_west", "growth", NAN, 1.0, 1.0),)}, "demand"),
        ({"segments": (SegmentBaseline("eu_west", "growth", INF, INF, 1.0),)}, "demand"),
        ({"segments": (SegmentBaseline("eu_west", "growth", 5.0, 4.0, 1.0),)}, "upper demand"),
        ({"segments": (SegmentBaseline("eu_west", "growth", 5.0, 6.0, 1.5),)}, "served share"),
        ({"segments": (SegmentBaseline("eu_west", "vip", 5.0, 6.0, 1.0),)}, "no elasticity"),
        ({"segments": (SegmentBaseline("mars", "growth", 5.0, 6.0, 1.0),)}, "no cost"),
        ({"regions": {"eu_west": RegionContext("eu_west", -1.0, 0.5),
                      "us_east": RegionContext("us_east", 1.0, 0.5)}}, "marginal cost"),
        ({"regions": {"eu_west": RegionContext("eu_west", NAN, 0.5),
                      "us_east": RegionContext("us_east", 1.0, 0.5)}}, "marginal cost"),
        ({"regions": {"eu_west": RegionContext("eu_west", 1.0, -0.1),
                      "us_east": RegionContext("us_east", 1.0, 0.5)}}, "utilisation"),
        ({"elasticity": {"starter": TierElasticity(NAN, 0.1), "growth": TierElasticity(-1, 0.1),
                         "enterprise": TierElasticity(-1, 0.1)}}, "elasticity of starter"),
        ({"elasticity": {"starter": TierElasticity(-1, -0.1), "growth": TierElasticity(-1, 0.1),
                         "enterprise": TierElasticity(-1, 0.1)}}, "elasticity of starter"),
        ({"churn": ChurnResponse(0.1, 0.05, 28, 10.0, 1.0)}, "upper bound"),
        ({"churn": ChurnResponse(0.1, 0.2, 0, 10.0, 1.0)}, "non-negative"),
        ({"churn": ChurnResponse(INF, INF, 28, 10.0, 1.0)}, "finite"),
        ({"product": "unknown_product"}, "no price bounds"),
    ],
)  # fmt: skip
def test_impossible_inputs_are_refused(overrides: dict[str, object], message: str) -> None:
    d = optimise(problem(**overrides), policy())
    assert d.status is Status.UNAVAILABLE
    assert d.reason_codes == ("invalid_input",)
    assert d.chosen_price_micros is None
    assert any(message in e for e in d.errors), d.errors
    d.record_json()  # never NaN in the audit record


def test_infinite_objective_is_refused() -> None:
    """Finite inputs whose objective overflows (exp of a huge exponent) never pick a price."""
    prob = single_segment(-1e6, 10_000.0, 100_000)
    d = optimise(prob, open_policy())
    assert d.status is Status.UNAVAILABLE
    assert d.reason_codes == ("invalid_objective",)


def test_infeasible_constraints_fail_safely() -> None:
    """The floor is above everything one step can reach: no candidate is feasible."""
    pol = policy(products={"api_requests": {"floor_micros": 500_000, "ceiling_micros": 900_000}})
    d = optimise(problem(), pol)
    assert d.status is Status.INFEASIBLE
    assert d.reason_codes == ("infeasible",)
    assert d.chosen_price_micros is None
    assert all(not c.feasible and "price_floor" in c.violations for c in d.candidates)
    assert d.guardrails["passed"] is False


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"anchor_price_micros": None}, "insufficient_evidence"),
        ({"evidence_date": None}, "insufficient_evidence"),
        ({"evidence_date": date(2024, 1, 1)}, "evidence_too_old"),
        ({"forecast_relative_width": 5.0}, "uncertainty_forecast_width"),
        (
            {"elasticity": {"starter": TierElasticity(-1.9, 0.03),
                            "growth": TierElasticity(0.3, 0.03),
                            "enterprise": TierElasticity(-0.7, 0.03)}},
            "implausible_elasticity",
        ),
        (
            {"elasticity": {"starter": TierElasticity(-1.9, 0.03),
                            "growth": TierElasticity(-1.2, 0.9),
                            "enterprise": TierElasticity(-0.7, 0.03)}},
            "uncertainty_elasticity_sd",
        ),
    ],
)  # fmt: skip
def test_weak_evidence_or_high_uncertainty_freezes_the_price(
    overrides: dict[str, object], reason: str
) -> None:
    d = optimise(problem(**overrides), policy())
    assert d.status is Status.FROZEN
    assert reason in d.reason_codes
    assert d.chosen_price_micros is None


def test_an_immaterial_tier_does_not_freeze_the_product() -> None:
    elasticity = {
        "starter": TierElasticity(-1.9, 0.9),  # imprecise, but only ~0.1% of demand
        "growth": TierElasticity(-1.2, 0.03),
        "enterprise": TierElasticity(-0.7, 0.03),
    }
    segments = (
        SegmentBaseline("eu_west", "starter", 10.0, 12.0, 1.0),
        SegmentBaseline("eu_west", "growth", 5_000.0, 6_000.0, 1.0),
        SegmentBaseline("eu_west", "enterprise", 8_000.0, 9_000.0, 1.0),
    )
    d = optimise(problem(elasticity=elasticity, segments=segments), policy())
    assert d.status is not Status.FROZEN


def test_low_confidence_freezes_an_otherwise_profitable_move() -> None:
    """Mean favours a cut, but elasticity is uncertain enough that a raise is plausible too."""
    prob = single_segment(-1.25, 10_000.0, 100_000, sd=0.2)
    pol = open_policy(max_step=0.05)
    pol = pol.model_copy(
        update={"uncertainty": pol.uncertainty.model_copy(update={"risk_aversion": 0.0})}
    )
    d = optimise(prob, pol)
    assert d.status is Status.FROZEN
    assert d.reason_codes == ("uncertainty_low_confidence",)
    assert d.prediction is not None and d.prediction["prob_improvement"] < 0.9


def test_mode_is_part_of_the_decision_identity() -> None:
    from praxis.pricing.config import Mode

    a = optimise(problem(), policy())
    b = optimise(problem(), policy(), mode=Mode.RECOMMEND)
    assert a.decision_id != b.decision_id
    assert a.mode is Mode.SHADOW and b.mode is Mode.RECOMMEND
