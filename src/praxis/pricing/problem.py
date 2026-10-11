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
class ChurnSegment:
    """The product's customers in one segment (tier in force), valued by the CLV model.

    ``churn_exposure`` = sum over the segment's customers of q_i * s_ip (calibrated baseline
    voluntary churn probability over the window x the product's share of the customer's
    bill): extra voluntary churners per unit of (response x log price change), first order.
    """

    segment: str
    exposed_customers: float  # active customers who used the product in the spend window
    clv_micros: float  # value of one lost customer of this segment (Phase 9 CLV model)
    window_churn: float  # expected voluntary churners over the window at the current price
    churn_exposure: float


@dataclass(frozen=True)
class ChurnResponse:
    """Causal voluntary-churn response to this product's price, and its valuation (ADR 0016).

    Extra voluntary churners of segment g over the window at log price change x:
    ``response * churn_exposure_g * x``. ``response`` is the bill elasticity of churn from the
    randomised tests (pooled across products); its SD also carries the uncertainty of the
    recent hazard calibration. Valued at each segment's CLV.
    """

    response: float  # posterior mean
    response_sd: float
    response_upper: float  # one-sided upper confidence bound (guardrails)
    window_days: int
    segments: tuple[ChurnSegment, ...] = ()
    # audit: how the response was built
    bill_response: float = 0.0  # pooled gamma_p (bill elasticity of voluntary churn)
    bill_response_sd: float = 0.0
    calibration_factor: float = 1.0  # recent observed / expected voluntary churn
    calibration_sd_log: float = 0.0
    bill_share: float = 1.0  # the product's mean bill share in the experiment that measured it
    # other credible posteriors (mean, SD) of the response: the decision must hold under each
    alternatives: tuple[tuple[float, float], ...] = ()

    @property
    def exposed_customers(self) -> float:
        return sum(g.exposed_customers for g in self.segments)

    @property
    def total_exposure(self) -> float:
        return sum(g.churn_exposure for g in self.segments)

    @property
    def value_at_risk(self) -> float:
        """sum_g CLV_g x churn_exposure_g: micro-GBP lost per unit of (response x log price)."""
        return sum(g.clv_micros * g.churn_exposure for g in self.segments)


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
    # exposed customers by the number of distinct dates with a price change they saw in the
    # policy's look-back window (customer-level price-change frequency, Phase 9)
    price_change_counts: Mapping[int, int] = field(default_factory=dict)
    # relative SD of the contribution CHANGE that the elasticity posterior does not carry: how
    # far this book's demand response can sit from the experiment population's (ADR 0016)
    contribution_rel_sd: float = 0.0

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
        errors += _change_count_errors(self.price_change_counts)
        if not (_finite(self.contribution_rel_sd) and 0.0 <= self.contribution_rel_sd < 1.0):
            errors.append("contribution relative SD must lie in [0, 1)")
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
        c.response,
        c.response_sd,
        c.response_upper,
        c.bill_response,
        c.bill_response_sd,
        c.calibration_factor,
        c.calibration_sd_log,
        c.bill_share,
    )
    if not all(_finite(v) for v in values):
        return ["churn response must be finite"]
    errors: list[str] = []
    if c.response_upper < c.response:
        errors.append("churn response upper bound below its point estimate")
    for mean, sd in c.alternatives:
        if not (_finite(mean) and _finite(sd)) or sd < 0:
            errors.append("alternative churn responses must be finite with sd >= 0")
    if c.window_days <= 0 or c.response_sd < 0 or c.calibration_factor < 0:
        errors.append("churn window, response SD and calibration must be non-negative")
    for g in c.segments:
        seg = (g.exposed_customers, g.clv_micros, g.window_churn, g.churn_exposure)
        if not all(_finite(v) for v in seg):
            errors.append(f"churn segment {g.segment} must be finite")
        elif min(seg) < 0 or g.window_churn > g.exposed_customers:
            errors.append(
                f"churn segment {g.segment}: counts, CLV and churn must be non-negative, "
                "churners at most the customers"
            )
    if len({g.segment for g in c.segments}) != len(c.segments):
        errors.append("churn segments must be unique")
    return errors


def _change_count_errors(counts: Mapping[int, int]) -> list[str]:
    if any(k < 0 or n < 0 for k, n in counts.items()):
        return ["price-change counts must be non-negative"]
    return []


@dataclass(frozen=True)
class Blocked:
    """No problem could be built for this product: why, and what is known."""

    product: str
    reason: str
    detail: str
    current_price_micros: int | None = None
    lineage: Mapping[str, str] = field(default_factory=dict)
