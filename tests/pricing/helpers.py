"""Small, fully known pricing problems and policies (SYNTHETIC)."""

from __future__ import annotations

from datetime import date
from typing import Any

from praxis.pricing.config import PricingPolicy, load_policy
from praxis.pricing.problem import (
    ChurnResponse,
    ChurnSegment,
    PricingProblem,
    RegionContext,
    SegmentBaseline,
    TierElasticity,
)

AS_OF = date(2026, 7, 20)
PRODUCT = "api_requests"


def policy(**sections: dict[str, Any]) -> PricingPolicy:
    """The registered policy with some fields overridden, e.g. ``constraints={"max_step": 0.1}``."""
    base = load_policy().model_dump(mode="json")
    for name, fields in sections.items():
        if name == "products":
            base["products"] = fields
        else:
            base[name] = {**base[name], **fields}
    return PricingPolicy.model_validate(base)


def churn(
    response: float,
    upper: float,
    window_days: int = 28,
    exposed: float = 0.0,
    clv_micros: float = 0.0,
    *,
    sd: float = 0.0,
    segment: str = "growth",
    window_churn: float = 0.0,
) -> ChurnResponse:
    """One segment whose customers carry the whole bill (churn_exposure = exposed customers).

    Then ``response`` is the extra churn probability per exposed customer per unit log price,
    exactly the Phase 6 "slope", and the CLV penalty is exposed x CLV x response x log ratio.
    """
    segments = (
        (ChurnSegment(segment, exposed, clv_micros, window_churn, exposed),) if exposed else ()
    )
    return ChurnResponse(response, sd, upper, window_days, segments)


def problem(**overrides: Any) -> PricingProblem:
    """A three-tier, two-region api_requests problem at 400,000 micros (all fields overridable)."""
    fields: dict[str, Any] = {
        "product": PRODUCT,
        "as_of": AS_OF,
        "current_price_micros": 400_000,
        "last_change": date(2026, 4, 1),
        "anchor_price_micros": 400_000,
        "evidence_date": date(2026, 2, 2),
        "elasticity": {
            "starter": TierElasticity(-1.9, 0.03),
            "growth": TierElasticity(-1.2, 0.03),
            "enterprise": TierElasticity(-0.7, 0.03),
        },
        "segments": (
            SegmentBaseline("eu_west", "starter", 2_000.0, 2_400.0, 1.0),
            SegmentBaseline("eu_west", "growth", 5_000.0, 6_000.0, 0.99),
            SegmentBaseline("eu_west", "enterprise", 8_000.0, 9_000.0, 1.0),
            SegmentBaseline("us_east", "growth", 3_000.0, 3_500.0, 1.0),
        ),
        "regions": {
            "eu_west": RegionContext("eu_west", 121_000.0, 0.6),
            "us_east": RegionContext("us_east", 110_000.0, 0.62),
        },
        "payment_loss_rate": 0.02,
        "churn": churn(0.02, 0.07, 28, 600.0, 4e8),
        "forecast_relative_width": 0.3,
        "lineage": {
            "forecast_model_version": "test",
            "elasticity_model_version": "test",
            "market_snapshot": "market-test",
            "customer_model_version": "churn-table-test",
            "customer_snapshot": "customers-test",
            "churn_response_version": "churn-test",
        },
    }
    fields.update(overrides)
    return PricingProblem(**fields)


def single_segment(
    elasticity: float, cost: float, price: int, *, sd: float = 0.0, **overrides: Any
) -> PricingProblem:
    """One region, one tier, no churn or payment loss: the textbook constant-elasticity case."""
    fields: dict[str, Any] = {
        "current_price_micros": price,
        "anchor_price_micros": price,
        "elasticity": {"growth": TierElasticity(elasticity, sd)},
        "segments": (SegmentBaseline("eu_west", "growth", 1_000.0, 1_000.0, 1.0),),
        "regions": {"eu_west": RegionContext("eu_west", cost, 0.5)},
        "payment_loss_rate": 0.0,
        "churn": churn(0.0, 0.0),
    }
    return problem(**{**fields, **overrides})


def open_policy(*, long_term: dict[str, Any] | None = None, **constraints: Any) -> PricingPolicy:
    """Loose bounds so that only the constraint under test can bind."""
    return policy(
        long_term={
            "max_segment_incremental_churn": {"starter": 0.99, "growth": 0.99, "enterprise": 0.99},
            "max_segment_window_churn": 1.0,
            "max_price_changes_per_customer": 1000,
            **(long_term or {}),
        },
        constraints={
            "max_step": 0.5,
            "cooldown_days": 0,
            "min_contribution_margin": 0.0,
            "max_utilization": 1.5,
            "max_incremental_churn": 0.99,
            **constraints,
        },
        evidence={"max_extrapolation": 5.0},
        products={PRODUCT: {"floor_micros": 1, "ceiling_micros": 10**9}},
        policy={"price_tick_micros": 1},
    )
