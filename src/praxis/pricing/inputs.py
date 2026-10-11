"""Build one ``PricingProblem`` per product from live inputs, or say exactly why not.

Sources: the demand forecast (Phase 4 service, at the CURRENT prices: no planned prices are
passed, its price features are predictive only), the causal evidence (Phase 5 elasticities,
Phase 9 churn response), the customer values (Phase 9: calibrated churn hazards, CLV, bill
shares, price-change history), the market snapshot (marts) and the price book (executed
decisions, for the cooldown). Each failure is mapped to a reason code (``Blocked``) and
becomes an explicit UNAVAILABLE decision: missing or stale forecasts, an unavailable model,
missing cost, price or customer values.

A decision at the start of day T uses data through T - 1 only (``CutoffFeatureSource``).
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol

import duckdb
import numpy as np
from numpy.typing import NDArray

from praxis.customer.artifact import ArtifactError as CustomerArtifactError
from praxis.customer.artifact import CustomerArtifact
from praxis.customer.config import CustomerValueConfig
from praxis.customer.value import CustomerValues, customer_values
from praxis.customer.warehouse import WarehouseUnavailable as CustomerWarehouseUnavailable
from praxis.customer.warehouse import connect as connect_customers
from praxis.forecasting.artifact import ArtifactError, ForecastArtifact
from praxis.forecasting.panel import SeriesKey
from praxis.forecasting.service import (
    FeatureSource,
    ForecastResult,
    ForecastService,
    ForecastUnavailable,
    Freshness,
    Snapshot,
)
from praxis.forecasting.warehouse import WarehouseUnavailable, connect
from praxis.pricing.config import PricingPolicy
from praxis.pricing.decision import Reason
from praxis.pricing.evidence import PricingEvidence
from praxis.pricing.problem import (
    Blocked,
    ChurnResponse,
    ChurnSegment,
    PricingProblem,
    RegionContext,
    SegmentBaseline,
    TierElasticity,
)
from praxis.pricing.store import DecisionStore, last_execution_date
from praxis.pricing.warehouse import MarketSnapshot, load_market

logger = logging.getLogger(__name__)


class Forecaster(Protocol):
    """What the pricing cycle needs from the forecast service (ForecastService satisfies it)."""

    def forecast(
        self,
        series: Sequence[SeriesKey] | None = None,
        horizons: Sequence[int] | None = None,
        planned_prices: Mapping[str, int] | None = None,
    ) -> ForecastResult: ...


CustomerSource = Callable[[date], CustomerValues]


class ProblemSource(Protocol):
    def problems(self, as_of: date) -> dict[str, PricingProblem | Blocked]: ...


class CutoffFeatureSource:
    """Wraps a feature source so it never returns data from on or after ``cutoff + 1``."""

    def __init__(self, inner: FeatureSource, cutoff: date) -> None:
        self.inner = inner
        self.cutoff = cutoff

    def snapshot(self, series: Sequence[SeriesKey], *, not_after: date) -> Snapshot | None:
        return self.inner.snapshot(series, not_after=min(not_after, self.cutoff))


def decision_clock(as_of: date) -> Callable[[], datetime]:
    """The forecast service's clock for a decision taken at the start of ``as_of`` (UTC)."""
    instant = datetime.combine(as_of, time(0, 5), tzinfo=UTC)
    return lambda: instant


@dataclass
class WarehouseProblemSource:
    """Live inputs: forecast service + evidence + marts + price book (each injected)."""

    policy: PricingPolicy
    warehouse: Path
    forecaster: Callable[[date], Forecaster]
    evidence: PricingEvidence | None
    customers: CustomerSource | None = None
    store: DecisionStore | None = None
    evidence_error: str = ""

    def problems(self, as_of: date) -> dict[str, PricingProblem | Blocked]:
        products = list(self.policy.products)
        if self.evidence is None:
            return _all(products, Reason.MODEL_UNAVAILABLE, self.evidence_error or "no evidence")
        try:
            con = connect(self.warehouse)
            try:
                market = load_market(con, as_of, self.policy.valuation)
            finally:
                con.close()
        except (WarehouseUnavailable, OSError, duckdb.Error) as exc:
            logger.warning("market snapshot unavailable", extra={"error": type(exc).__name__})
            return _all(products, Reason.INPUT_UNAVAILABLE, f"warehouse: {type(exc).__name__}")
        forecast = self._forecast(as_of)
        if isinstance(forecast, Blocked):
            return _all(products, forecast.reason, forecast.detail, market)
        values = self._customers(as_of)
        if isinstance(values, Blocked):
            return _all(products, values.reason, values.detail, market)
        ev = self.evidence
        shares = reference_shares(values, _price_ratios(values, market, ev), ev.tiers)
        return {p: self._problem(p, as_of, market, forecast, ev, values, shares) for p in products}

    def _customers(self, as_of: date) -> CustomerValues | Blocked:
        if self.customers is None:
            return Blocked("*", Reason.MODEL_UNAVAILABLE, "no customer value model")
        try:
            return self.customers(as_of)
        except (CustomerArtifactError, OSError) as exc:
            logger.warning("customer model unavailable", extra={"error": type(exc).__name__})
            return Blocked("*", Reason.MODEL_UNAVAILABLE, f"customer model: {type(exc).__name__}")
        except (CustomerWarehouseUnavailable, duckdb.Error, ValueError) as exc:
            logger.warning("customer values unavailable", extra={"error": type(exc).__name__})
            return Blocked("*", Reason.INPUT_UNAVAILABLE, f"customer values: {type(exc).__name__}")

    def _forecast(self, as_of: date) -> ForecastResult | Blocked:
        try:
            service = self.forecaster(as_of)
            result = service.forecast(horizons=list(range(1, self.policy.policy.horizon_days + 1)))
        except ForecastUnavailable as exc:
            return Blocked("*", Reason.FORECAST_UNAVAILABLE, f"{exc.code}: {exc}")
        except (ArtifactError, OSError, ValueError, duckdb.Error) as exc:
            logger.warning("forecast model unavailable", extra={"error": type(exc).__name__})
            return Blocked("*", Reason.MODEL_UNAVAILABLE, f"forecast: {type(exc).__name__}")
        if result.freshness is not Freshness.FRESH and self.policy.evidence.require_fresh_forecast:
            return Blocked(
                "*",
                Reason.FORECAST_STALE,
                f"features {result.feature_lag_days} days old ({result.fallback_reason})",
            )
        return result

    def _problem(
        self,
        product: str,
        as_of: date,
        market: MarketSnapshot,
        forecast: ForecastResult,
        evidence: PricingEvidence,
        values: CustomerValues,
        shares: NDArray[np.float64],
    ) -> PricingProblem | Blocked:
        price = market.plan.price_on(product, market.cutoff)
        lineage = _lineage(market, forecast, evidence, values)
        if price is None:
            return Blocked(product, Reason.PRICE_UNAVAILABLE, "no list price", None, lineage)
        segments, width = _segments(product, forecast, market, self.policy)
        if not segments:
            return Blocked(product, Reason.FORECAST_MISSING, "no forecast series", price, lineage)
        regions = {}
        for region in sorted({s.region for s in segments}):
            cost = market.unit_cost.get((region, product))
            util = market.utilization.get(region)
            if cost is None:
                return Blocked(product, Reason.COST_UNAVAILABLE, region, price, lineage)
            if util is None:
                return Blocked(
                    product, Reason.INPUT_UNAVAILABLE, f"utilisation {region}", price, lineage
                )
            regions[region] = RegionContext(region, cost, util)
        if market.payment_loss_rate is None:
            return Blocked(product, Reason.INPUT_UNAVAILABLE, "payments", price, lineage)
        churn = churn_response(product, values, evidence, self.policy, shares)
        ev = evidence.products.get(product)
        return PricingProblem(
            product=product,
            as_of=as_of,
            current_price_micros=price,
            last_change=self._last_change(product, market),
            anchor_price_micros=None
            if ev is None
            else market.plan.price_on(product, ev.assignment_date),
            evidence_date=None if ev is None else ev.assignment_date,
            elasticity=book_elasticity(product, values, evidence),
            segments=tuple(segments),
            regions=regions,
            payment_loss_rate=market.payment_loss_rate,
            churn=churn,
            forecast_relative_width=width,
            lineage=lineage,
            price_change_counts=change_counts(product, values, self.policy),
        )

    def _last_change(self, product: str, market: MarketSnapshot) -> date | None:
        points = market.plan.changes.get(product, ())
        observed = points[-1][0] if len(points) > 1 else None  # the first point is the launch
        executed = None if self.store is None else last_execution_date(self.store, product)
        dates = [d for d in (observed, executed) if d is not None]
        return max(dates) if dates else None


def _price_ratios(
    values: CustomerValues, market: MarketSnapshot, evidence: PricingEvidence
) -> dict[str, float]:
    """Current list price / list price when the product's churn response was measured."""
    out: dict[str, float] = {}
    for p in values.snapshot.products:
        ev = evidence.products.get(p)
        now = market.plan.price_on(p, market.cutoff)
        then = None if ev is None else market.plan.price_on(p, ev.assignment_date)
        out[p] = now / then if now and then else 1.0
    return out


def reference_shares(
    values: CustomerValues, price_ratio: Mapping[str, float], tiers: Mapping[str, TierElasticity]
) -> NDArray[np.float64]:
    """(n, P) Laspeyres bill shares: the reference basket, valued at today's prices.

    The churn response was measured against pre-period bill shares at the TESTED prices. Today's
    usage already reflects every list-price change since (customers buy less of what got
    dearer), so today's spend share understates the exposure of a raised product. The basket
    the customer would buy at the tested prices is today's quantity x (p_tested / p_now)^e
    (e: the customer's tier elasticity, causal, Phase 5); valued at today's prices its spend is
    today's spend x (p_tested / p_now)^e. ADR 0016 second amendment.
    """
    snap = values.snapshot
    ratio = np.array([price_ratio.get(p, 1.0) for p in snap.products])
    e = np.array([tiers[t].mean if t in tiers else 0.0 for t in snap.tier])
    value = snap.value_micros * (1.0 / ratio)[None, :] ** e[:, None]
    total = value.sum(axis=1, keepdims=True)
    out: NDArray[np.float64] = np.divide(value, total, out=np.zeros_like(value), where=total > 0)
    return out


def _exposed(
    product: str, values: CustomerValues, shares: NDArray[np.float64] | None = None
) -> NDArray[np.float64]:
    snap = values.snapshot
    if product not in snap.products:
        return np.zeros(snap.n)
    table = snap.shares() if shares is None else shares
    out: NDArray[np.float64] = table[:, snap.products.index(product)]
    return out


def book_elasticity(
    product: str, values: CustomerValues, evidence: PricingEvidence
) -> dict[str, TierElasticity]:
    """Tier elasticities as they apply to THIS book of customers (ADR 0016 second amendment).

    The Phase 5 posterior is for the population mean. The book's demand-weighted elasticity in
    tier t deviates from it with SD eta_t = sqrt(Var_t(e_i) x sum_i w_i^2) (w_i: the
    customer's share of the tier's spend on the product; Var_t from the randomised arms), so
    the predictive SD is sqrt(posterior SD^2 + eta_t^2).
    """
    snap = values.snapshot
    k = snap.products.index(product) if product in snap.products else None
    tiers = np.array(snap.initial_tier or snap.tier)
    out: dict[str, TierElasticity] = {}
    for tier, e in evidence.tiers.items():
        var = evidence.heterogeneity.get(tier, 0.0)
        eta = 0.0
        if k is not None and var > 0:
            w = snap.value_micros[tiers == tier, k]
            if w.sum() > 0:
                share = w / w.sum()
                eta = math.sqrt(var * float((share**2).sum()))
        out[tier] = TierElasticity(e.mean, math.hypot(e.sd, eta))
    return out


def churn_response(
    product: str,
    values: CustomerValues,
    evidence: PricingEvidence,
    policy: PricingPolicy,
    shares: NDArray[np.float64] | None = None,
) -> ChurnResponse:
    """Segment churn exposure and valuation of ``product`` (ADR 0016).

    Customer i's extra voluntary churn over the window at log price change x is
    q_i * gamma_p * s_ip * x (first order): q_i its calibrated baseline window churn, s_ip the
    product's share of its bill. A segment's value per lost customer is the CLV of the
    customers the price would lose (weighted by q_i s_ip), its segment mean if none.
    """
    s = _exposed(product, values, shares)
    q = values.window_churn()
    tiers = np.array(values.snapshot.tier)
    segments = []
    for name in sorted(set(values.snapshot.tier)):
        m = (tiers == name) & (s > 0)
        weight = q[m] * s[m]
        clv = values.clv_micros[m]
        value = float((clv * weight).sum() / weight.sum()) if weight.sum() > 0 else 0.0
        if weight.sum() <= 0 and m.any():
            value = float(clv.mean())
        segments.append(
            ChurnSegment(
                name, float(m.sum()), max(value, 0.0), float(q[m].sum()), float(weight.sum())
            )
        )
    ev = evidence.products.get(product)
    cal = values.calibration
    sd_log = 0.0 if cal is None else cal.sd_log
    factor = 1.0 if cal is None else cal.factor
    if ev is None:  # untested: frozen by the evidence gate anyway
        return ChurnResponse(0.0, 0.0, 0.0, values.window_days, tuple(segments))
    mean = ev.churn_response
    # the calibration multiplies every q_i: its log-SD is a relative SD of the response
    sd = math.hypot(ev.churn_response_sd, abs(mean) * sd_log)
    others = tuple((m, math.hypot(s, abs(m) * sd_log)) for _, m, s in ev.churn_models[1:])
    z = policy.evidence.churn_upper_z
    # guardrails use the most pessimistic upper bound of the credible class
    upper = max(m + z * s for m, s in ((mean, sd), *others))
    return ChurnResponse(
        response=mean,
        response_sd=sd,
        response_upper=max(upper, mean),
        alternatives=others,
        window_days=values.window_days,
        segments=tuple(segments),
        bill_response=ev.churn_response,
        bill_response_sd=ev.churn_response_sd,
        calibration_factor=factor,
        calibration_sd_log=sd_log,
        bill_share=ev.bill_share,
    )


def change_counts(product: str, values: CustomerValues, policy: PricingPolicy) -> dict[int, int]:
    """Exposed customers by distinct price-change dates in the policy's look-back window."""
    exposed = _exposed(product, values) > 0
    counts = values.snapshot.price_changes_within(policy.long_term.price_change_window_days)
    out: dict[int, int] = defaultdict(int)
    for k in counts[exposed].tolist():
        out[int(k)] += 1
    return dict(sorted(out.items()))


def _lineage(
    market: MarketSnapshot,
    forecast: ForecastResult,
    evidence: PricingEvidence,
    values: CustomerValues,
) -> dict[str, str]:
    return {
        "customer_model_version": values.model_version,
        "customer_snapshot": values.snapshot.version(),
        "churn_response_version": evidence.churn_response_version,
        "churn_archive_version": evidence.churn_archive_version,
        "forecast_model_version": forecast.model_version,
        "forecast_feature_version": forecast.feature_version,
        "forecast_feature_date": forecast.feature_date.isoformat(),
        "forecast_freshness": forecast.freshness.value,
        "elasticity_model_version": evidence.model_version,
        "elasticity_data_version": evidence.data_version,
        "market_snapshot": market.version(),
        "price_plan_version": market.plan.version(),
    }


def _segments(
    product: str, forecast: ForecastResult, market: MarketSnapshot, policy: PricingPolicy
) -> tuple[list[SegmentBaseline], float]:
    """Mean daily forecast per (region, tier) over the horizon, and the relative width."""
    levels = forecast.quantile_levels
    q_cap = min(
        (q for q in levels if q >= policy.constraints.capacity_quantile), default=max(levels)
    )
    q_lo, q_hi = min(levels, key=lambda q: abs(q - 0.1)), min(levels, key=lambda q: abs(q - 0.9))
    sums: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    total = lo = hi = 0.0
    for pt in forecast.points:
        if pt.series.product != product:
            continue
        acc = sums[(pt.series.region_id, pt.series.segment)]
        acc[0] += max(pt.point, 0.0)
        acc[1] += max(pt.quantiles[q_cap], pt.point, 0.0)
        acc[2] += 1
        total += max(pt.point, 0.0)
        lo += max(pt.quantiles[q_lo], 0.0)
        hi += max(pt.quantiles[q_hi], 0.0)
    segments = [
        SegmentBaseline(
            region,
            tier,
            demand=acc[0] / acc[2],
            demand_high=acc[1] / acc[2],
            served_share=market.served_share.get((region, product, tier), 1.0),
        )
        for (region, tier), acc in sorted(sums.items())
    ]
    width = (hi - lo) / total if total > 0 else 0.0
    return segments, width


def _all(
    products: list[str], reason: str, detail: str, market: MarketSnapshot | None = None
) -> dict[str, PricingProblem | Blocked]:
    out: dict[str, PricingProblem | Blocked] = {}
    for p in products:
        price = None if market is None else market.plan.price_on(p, market.cutoff)
        out[p] = Blocked(p, reason, detail, price)
    return out


def forecast_factory(
    artifact_loader: Callable[[], ForecastArtifact], source: FeatureSource
) -> Callable[[date], ForecastService]:
    """Forecast service per decision date: clock at the start of the day, data before it."""

    def make(as_of: date) -> ForecastService:
        return ForecastService(
            artifact_loader(),
            CutoffFeatureSource(source, as_of - timedelta(days=1)),
            clock=decision_clock(as_of),
        )

    return make


def customer_factory(
    warehouse: Path,
    artifact_loader: Callable[[], CustomerArtifact],
    cfg: CustomerValueConfig,
    policy: PricingPolicy,
) -> CustomerSource:
    """Customer values per decision date (snapshot strictly before it; Phase 9)."""

    def make(as_of: date) -> CustomerValues:
        con = connect_customers(warehouse)
        try:
            return customer_values(
                con,
                artifact_loader(),
                as_of,
                cfg,
                price_history_days=policy.long_term.price_change_window_days,
            )
        finally:
            con.close()

    return make
