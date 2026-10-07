"""Build one ``PricingProblem`` per product from live inputs, or say exactly why not.

Sources: the demand forecast (Phase 4 service, at the CURRENT prices: no planned prices are
passed, its price features are predictive only), the causal evidence (Phase 5), the market
snapshot (marts) and the price book (executed decisions, for the cooldown). Each failure is
mapped to a reason code (``Blocked``) and becomes an explicit UNAVAILABLE decision: missing or
stale forecasts, an unavailable model, missing cost or price.

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
    PricingProblem,
    RegionContext,
    SegmentBaseline,
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
        ev = self.evidence
        return {p: self._problem(p, as_of, market, forecast, ev) for p in products}

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
    ) -> PricingProblem | Blocked:
        price = market.plan.price_on(product, market.cutoff)
        lineage = _lineage(market, forecast, evidence)
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
        churn = self._churn(product, market, evidence)
        if market.payment_loss_rate is None or churn is None:
            return Blocked(
                product, Reason.INPUT_UNAVAILABLE, "payments / customers", price, lineage
            )
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
            elasticity=evidence.tiers,
            segments=tuple(segments),
            regions=regions,
            payment_loss_rate=market.payment_loss_rate,
            churn=churn,
            forecast_relative_width=width,
            lineage=lineage,
        )

    def _churn(
        self, product: str, market: MarketSnapshot, evidence: PricingEvidence
    ) -> ChurnResponse | None:
        clv = market.clv_micros(self.policy.valuation.clv_lifetime_cap_days)
        if clv is None:
            return None
        ev = evidence.products.get(product)
        exposed = float(market.exposed_customers.get(product, 0))
        if ev is None or market.daily_churn_hazard is None:
            return ChurnResponse(0.0, 0.0, 28, exposed, clv)  # untested: frozen anyway
        # Price multiplies the hazard: absolute effect = relative effect x CURRENT churn rate.
        base = 1.0 - math.exp(-ev.window_days * market.daily_churn_hazard)
        upper = ev.churn_rel_upper(self.policy.evidence.churn_upper_z)
        return ChurnResponse(
            slope=ev.churn_rel_slope * base,
            slope_upper=max(upper, ev.churn_rel_slope) * base,
            window_days=ev.window_days,
            exposed_customers=exposed,
            clv_micros=clv,
            slope_sd=ev.churn_rel_se * base,
            relative_slope=ev.churn_rel_slope,
            base_window_churn=base,
        )

    def _last_change(self, product: str, market: MarketSnapshot) -> date | None:
        points = market.plan.changes.get(product, ())
        observed = points[-1][0] if len(points) > 1 else None  # the first point is the launch
        executed = None if self.store is None else last_execution_date(self.store, product)
        dates = [d for d in (observed, executed) if d is not None]
        return max(dates) if dates else None


def _lineage(
    market: MarketSnapshot, forecast: ForecastResult, evidence: PricingEvidence
) -> dict[str, str]:
    return {
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
