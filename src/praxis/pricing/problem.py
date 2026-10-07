"""The pricing problem for one product in one cycle: everything the optimiser may use.

Pure data, no I/O. ``praxis.pricing.inputs`` builds it from the forecast service, the
elasticity evidence and the warehouse; tests build it directly. ``validate`` names every
impossible input (negative demand, non-positive price, NaN, ...), so a bad input becomes an
explicit, safe ``unavailable`` decision rather than a number.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class TierElasticity:
    """Causal own-price elasticity of a customer tier (posterior mean and SD; Phase 5)."""

    mean: float
    sd: float


@dataclass(frozen=True)
class SegmentBaseline:
    """Forecast demand of one (region, tier) at the CURRENT price, per day of the horizon."""

    region: str
    tier: str
    demand: float  # expected requested units per day (forecast point)
    demand_high: float  # upper forecast quantile per day (capacity check)
    served_share: float  # recent share of requested units actually served, in [0, 1]


@dataclass(frozen=True)
class RegionContext:
    region: str
    unit_cost_micros: float  # recent marginal cost of this product in the region
    utilization: float  # recent peak daily-mean utilisation of the region (all products)


@dataclass(frozen=True)
class ChurnResponse:
    """Causal churn response to this product's price (randomised tests) and its valuation."""

    slope: float  # d P(churn within window) / d log price (posterior mean)
    slope_upper: float  # one-sided upper confidence bound of the slope (guardrail)
    window_days: int  # the evidence window the probabilities refer to
    exposed_customers: float  # active customers who use this product
    clv_micros: float  # CLV proxy per customer (valuation of one lost customer)
    slope_sd: float = 0.0  # posterior SD of the slope (enters the decision's uncertainty)
    # Audit of how the absolute slope was built: relative slope x current window churn rate.
    relative_slope: float = 0.0  # d log(churn rate) / d log price (posterior mean)
    base_window_churn: float = 0.0  # churn probability over the window observed before as_of


@dataclass(frozen=True)
class PricingProblem:
    product: str
    as_of: date  # decision date; every input uses data strictly before it
    current_price_micros: int
    last_change: date | None  # last list-price change of this product
    anchor_price_micros: int | None  # list price when the elasticity was tested
    evidence_date: date | None  # assignment date of the product's randomised test
    elasticity: Mapping[str, TierElasticity]
    segments: tuple[SegmentBaseline, ...]
    regions: Mapping[str, RegionContext]
    payment_loss_rate: float
    churn: ChurnResponse
    forecast_relative_width: float  # (q90 - q10) / point of the product's total demand
    lineage: Mapping[str, str] = field(default_factory=dict)  # input versions / snapshot refs

    @property
    def total_demand(self) -> float:
        return sum(s.demand for s in self.segments)

    def tier_shares(self) -> dict[str, float]:
        total = self.total_demand
        out: dict[str, float] = {}
        for s in self.segments:
            out[s.tier] = out.get(s.tier, 0.0) + (s.demand / total if total > 0 else 0.0)
        return out

    def validate(self) -> list[str]:
        """Human-readable reasons this problem is impossible (empty when it is usable)."""
        errors: list[str] = []
        if self.current_price_micros <= 0:
            errors.append("current price must be positive")
        if not self.segments:
            errors.append("no forecast segments")
        if self.anchor_price_micros is not None and self.anchor_price_micros <= 0:
            errors.append("anchor price must be positive")
        if not (_finite(self.payment_loss_rate) and 0.0 <= self.payment_loss_rate < 1.0):
            errors.append("payment loss rate must be in [0, 1)")
        if not (_finite(self.forecast_relative_width) and self.forecast_relative_width >= 0):
            errors.append("forecast relative width must be finite and >= 0")
        errors += _segment_errors(self)
        errors += _churn_errors(self.churn)
        for tier, e in self.elasticity.items():
            if not (_finite(e.mean) and _finite(e.sd) and e.sd >= 0):
                errors.append(f"elasticity of {tier} must be finite with sd >= 0")
        return errors


def _finite(x: float) -> bool:
    return isinstance(x, int | float) and math.isfinite(x)


def _segment_errors(p: PricingProblem) -> list[str]:
    errors: list[str] = []
    for s in p.segments:
        label = f"{s.region}/{s.tier}"
        if not (_finite(s.demand) and _finite(s.demand_high)) or s.demand < 0:
            errors.append(f"{label}: demand must be finite and >= 0")
        elif s.demand_high < s.demand:
            errors.append(f"{label}: upper demand quantile below the point forecast")
        if not (_finite(s.served_share) and 0.0 <= s.served_share <= 1.0):
            errors.append(f"{label}: served share must be in [0, 1]")
        if s.tier not in p.elasticity:
            errors.append(f"{label}: no elasticity for tier {s.tier}")
        ctx = p.regions.get(s.region)
        if ctx is None:
            errors.append(f"{label}: no cost / capacity context for the region")
        elif not (_finite(ctx.unit_cost_micros) and ctx.unit_cost_micros >= 0):
            errors.append(f"{s.region}: marginal cost must be finite and >= 0")
        elif not (_finite(ctx.utilization) and ctx.utilization >= 0):
            errors.append(f"{s.region}: utilisation must be finite and >= 0")
    return errors


def _churn_errors(c: ChurnResponse) -> list[str]:
    values = (
        c.slope,
        c.slope_upper,
        c.exposed_customers,
        c.clv_micros,
        c.slope_sd,
        c.relative_slope,
        c.base_window_churn,
    )
    if not all(_finite(v) for v in values):
        return ["churn response must be finite"]
    errors: list[str] = []
    if c.slope_upper < c.slope:
        errors.append("churn slope upper bound below its point estimate")
    if not 0.0 <= c.base_window_churn <= 1.0:
        errors.append("base window churn must be a probability")
    if c.window_days <= 0 or c.exposed_customers < 0 or c.clv_micros < 0 or c.slope_sd < 0:
        errors.append("churn window, exposed customers and CLV must be non-negative")
    return errors


@dataclass(frozen=True)
class Blocked:
    """No problem could be built for this product: why, and what is known."""

    product: str
    reason: str
    detail: str
    current_price_micros: int | None = None
    lineage: Mapping[str, str] = field(default_factory=dict)
