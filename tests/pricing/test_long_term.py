"""Long-horizon guardrails alter unsafe short-term decisions (required_test.md s15, Phase 9).

Each case is a raise that improves IMMEDIATE contribution: without the long-term terms the
optimiser takes it; with them it must reject or shrink it, with the binding reason recorded.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from praxis.pricing.decision import Status
from praxis.pricing.optimiser import optimise
from praxis.pricing.problem import ChurnResponse, ChurnSegment, PricingProblem
from tests.pricing.helpers import open_policy, single_segment

P0 = 100_000
UPPER = 110_000  # max step 10% in these cases


def raise_problem(churn: ChurnResponse, **kw: Any) -> PricingProblem:
    """Inelastic demand (-0.5), cost far below price: every raise adds contribution."""
    return single_segment(-0.5, 10_000.0, P0, churn=churn, **kw)


def segments(**override: Any) -> tuple[ChurnSegment, ...]:
    """Starter churns a lot and is worth GBP 20; enterprise churns little, worth GBP 12k.

    The +10% raise adds ~GBP 5 / day of contribution (1,000 units at 0.1 GBP, elasticity -0.5).
    """
    seg = {
        "starter": ChurnSegment("starter", 500.0, 2e7, 30.0, 15.0),
        "enterprise": ChurnSegment("enterprise", 100.0, 1.2e10, 2.0, 1.5),
    }
    for name, fields in override.items():
        seg[name] = ChurnSegment(name, *fields)
    return tuple(seg.values())


def churn(response: float, sd: float = 0.0, **override: Any) -> ChurnResponse:
    return ChurnResponse(response, sd, response + 1.6449 * sd, 28, segments(**override))


def short_term_policy(**long_term: Any):  # type: ignore[no-untyped-def]
    return open_policy(max_step=0.1, long_term=long_term)


def test_short_term_optimum_is_the_largest_raise() -> None:
    d = optimise(raise_problem(churn(0.0)), short_term_policy())
    assert d.status is Status.CHANGE and d.chosen_price_micros == UPPER
    assert d.prediction is not None and d.prediction["delta_contribution_mean"] > 0


def test_the_clv_penalty_rejects_a_raise_that_loses_valuable_customers() -> None:
    """Same immediate gain, but each lost enterprise customer is worth GBP 120k."""
    prob = raise_problem(churn(2.0, 0.3))
    d = optimise(prob, short_term_policy())
    # the raise is rejected (by symmetry a cut, which keeps valuable customers, may win)
    assert d.chosen_price_micros is None or d.chosen_price_micros < P0
    hi = max(d.candidates, key=lambda c: c.price_micros)
    assert hi.delta_contribution_mean > 0 > hi.delta_mean  # short-term up, long-term down
    # valued at the cheap segment only, the same raise would go through
    cheap = raise_problem(churn(2.0, 0.3, enterprise=(100.0, 1e6, 2.0, 1.5)))
    assert optimise(cheap, short_term_policy()).status is Status.CHANGE


def test_the_segment_guardrail_shrinks_the_raise() -> None:
    """Enterprise may lose at most 0.3 pp: the raise is cut back to where that binds."""
    prob = raise_problem(churn(1.0, 0.0, enterprise=(100.0, 0.0, 2.0, 15.0)))
    free = optimise(prob, short_term_policy())
    capped = optimise(prob, short_term_policy(max_segment_incremental_churn={"enterprise": 0.003}))
    assert free.chosen_price_micros == UPPER
    assert capped.status is Status.CHANGE
    assert capped.chosen_price_micros is not None and P0 < capped.chosen_price_micros < UPPER
    # extra churn per enterprise customer = 1.0 x 15 / 100 x log ratio <= 0.003
    assert 0.15 * math.log(capped.chosen_price_micros / P0) <= 0.003 + 1e-12
    assert "segment_churn_guardrail" in capped.reason_codes
    seg = capped.guardrails["segments"]["enterprise"]
    assert seg["projected_incremental_churn"] <= seg["limit"]


def test_the_churn_risk_ceiling_blocks_raises_on_a_segment_already_losing_customers() -> None:
    # 11% of starters churn already; churn is cheap here, so only the ceiling can stop a raise
    hot = churn(0.5, 0.0, starter=(500.0, 2e7, 55.0, 15.0), enterprise=(100.0, 1e6, 2.0, 1.5))
    d = optimise(raise_problem(hot), short_term_policy(max_segment_window_churn=0.10))
    assert d.status is Status.HOLD and d.chosen_price_micros is None
    assert all("churn_risk_ceiling" in c.violations for c in d.candidates if c.price_micros > P0)
    free = optimise(raise_problem(hot), short_term_policy(max_segment_window_churn=1.0))
    assert free.chosen_price_micros is not None and free.chosen_price_micros > P0
    # a CUT stays allowed under the ceiling (it lowers churn)
    elastic = single_segment(-3.0, 10_000.0, P0, churn=hot)
    cut = optimise(elastic, short_term_policy(max_segment_window_churn=0.10))
    assert cut.status is Status.CHANGE and cut.chosen_price_micros is not None
    assert cut.chosen_price_micros < P0


def test_customer_price_change_frequency_freezes_the_list_price() -> None:
    prob = raise_problem(churn(0.0), price_change_counts={0: 400, 2: 3})
    d = optimise(prob, short_term_policy(max_price_changes_per_customer=3))
    assert d.chosen_price_micros == UPPER  # 2 + 1 = 3 changes: allowed
    blocked = raise_problem(churn(0.0), price_change_counts={0: 400, 3: 1})
    d = optimise(blocked, short_term_policy(max_price_changes_per_customer=3))
    assert d.status is Status.HOLD and d.chosen_price_micros is None
    assert d.guardrails["customers_over_change_limit"] == 1
    assert all(
        "price_change_frequency" in c.violations for c in d.candidates if c.price_micros != P0
    )


def test_a_product_with_a_tiny_bill_share_is_frozen() -> None:
    """ADR 0016 amendment: below 5% of the bill a +-18% test cannot identify the response."""
    from dataclasses import replace

    from praxis.pricing.decision import Reason

    base = churn(0.0)
    small = raise_problem(replace(base, bill_share=0.022))
    d = optimise(small, short_term_policy())
    assert d.status is Status.FROZEN and Reason.CHURN_RESPONSE_UNIDENTIFIED in d.reason_codes
    assert d.chosen_price_micros is None and not d.candidates
    at_limit = optimise(raise_problem(replace(base, bill_share=0.05)), short_term_policy())
    assert at_limit.status is Status.CHANGE
    assert d.record()["inputs"]["churn"]["bill_share"] == 0.022


def test_a_move_must_be_confident_under_every_credible_churn_posterior() -> None:
    """Maxmin over the class (ADR 0016 second amendment): one pessimistic member blocks."""
    from dataclasses import replace

    optimistic = churn(0.0, 0.0)  # the primary posterior says churn does not respond
    assert optimise(raise_problem(optimistic), short_term_policy()).status is Status.CHANGE
    # complete pooling says it does, strongly enough that the raise loses value
    robust = replace(optimistic, alternatives=((3.0, 0.2),))
    d = optimise(raise_problem(robust), short_term_policy())
    assert d.chosen_price_micros is None or d.chosen_price_micros < P0
    hi = max(d.candidates, key=lambda c: c.price_micros)
    assert hi.delta_mean < 0 and hi.prob_improvement < 0.5  # scored under the worst member
    # an alternative that agrees changes nothing
    agree = replace(optimistic, alternatives=((0.0, 0.0),))
    assert optimise(raise_problem(agree), short_term_policy()).chosen_price_micros == UPPER


def test_contribution_uncertainty_lowers_confidence_but_not_the_expectation() -> None:
    from dataclasses import replace

    base = raise_problem(churn(0.0))
    sure = optimise(base, short_term_policy())
    unsure = optimise(replace(base, contribution_rel_sd=0.5), short_term_policy())
    hi_sure = max(sure.candidates, key=lambda c: c.price_micros)
    hi_unsure = max(unsure.candidates, key=lambda c: c.price_micros)
    assert hi_unsure.delta_mean == pytest.approx(hi_sure.delta_mean, rel=1e-9)
    assert hi_unsure.delta_sd > hi_sure.delta_sd
    assert replace(base, contribution_rel_sd=1.5).validate()
