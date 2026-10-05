"""Forecast serving: freshness policy, stale-feature fallback, validation and metrics.

Feature lag = today (UTC) - latest complete feature day (ADR 0010):

* lag <= ``fresh_max_lag_days``: the LightGBM model serves (``fresh``);
* lag <= ``stale_max_lag_days``: the seasonal moving-average fallback serves (``stale``);
* otherwise, or with no complete day at all: ``ForecastUnavailable`` (no forecast).

Every call is timed and counted; every outcome is logged with the model version.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from praxis.forecasting.artifact import ForecastArtifact
from praxis.forecasting.features import WINDOW, build_features
from praxis.forecasting.models import Prediction
from praxis.forecasting.panel import DemandPanel, PricePlan, SeriesKey
from praxis.forecasting.warehouse import (
    WarehouseUnavailable,
    connect,
    latest_complete_day,
    load_panel,
    load_price_plan,
)
from praxis.observability import LatencySample

logger = logging.getLogger(__name__)


class Freshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"


class Source(StrEnum):
    MODEL = "model"
    FALLBACK = "fallback_baseline"


class ForecastUnavailable(RuntimeError):
    """No trustworthy forecast can be produced (caller should treat as 503)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ForecastRequestError(ValueError):
    """The request itself is invalid (caller should treat as 4xx)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Snapshot:
    panel: DemandPanel  # ends at the feature date
    plan: PricePlan

    @property
    def feature_date(self) -> date:
        return self.panel.end_date


class FeatureSource(Protocol):
    def snapshot(self, series: Sequence[SeriesKey], *, not_after: date) -> Snapshot | None:
        """Latest complete ``WINDOW``-day panel ending on or before ``not_after``."""
        ...


class WarehouseFeatureSource:
    """Reads the DuckDB marts read-only for each call (no long-lived lock on the file)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def snapshot(self, series: Sequence[SeriesKey], *, not_after: date) -> Snapshot | None:
        con = connect(self.path)
        try:
            feature_date = latest_complete_day(con, not_after=not_after)
            if feature_date is None:
                return None
            start = feature_date - timedelta(days=WINDOW - 1)
            panel = load_panel(con, start, feature_date, series=series)
            return Snapshot(panel, load_price_plan(con, until=feature_date))
        finally:
            con.close()


@dataclass(frozen=True)
class ForecastPoint:
    series: SeriesKey
    horizon_days: int
    target_date: date
    point: float
    quantiles: dict[float, float]


@dataclass(frozen=True)
class ForecastResult:
    model_name: str
    model_version: str
    feature_version: str
    created_at: datetime
    feature_date: date
    feature_cutoff: datetime  # every observation used is strictly before this instant
    feature_lag_days: int
    freshness: Freshness
    source: Source
    fallback_reason: str | None
    quantile_levels: tuple[float, ...]
    points: list[ForecastPoint]


@dataclass
class ForecastMetrics:
    """In-process counters + latency (Phase 11 exports these through OpenTelemetry)."""

    counters: Counter[str] = field(default_factory=Counter)
    latency: LatencySample = field(default_factory=LatencySample)
    last: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, outcome: str, elapsed_ms: float, **labels: Any) -> None:
        with self._lock:
            self.counters["forecast_requests_total"] += 1
            self.counters[f"forecast_outcome_total:{outcome}"] += 1
            for key in ("source", "freshness", "reason"):
                if labels.get(key):
                    self.counters[f"forecast_{key}_total:{labels[key]}"] += 1
            self.counters["forecast_points_total"] += int(labels.get("points", 0))
            self.latency.add(elapsed_ms)
            self.last = {"outcome": outcome, "latency_ms": round(elapsed_ms, 3), **labels}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(sorted(self.counters.items())),
                "latency": self.latency.summary(),
                "last": dict(self.last),
            }


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ForecastService:
    def __init__(
        self,
        artifact: ForecastArtifact,
        source: FeatureSource,
        *,
        clock: Callable[[], datetime] = _utc_now,
        metrics: ForecastMetrics | None = None,
    ) -> None:
        self.artifact = artifact
        self.source = source
        self.clock = clock
        self.metrics = metrics or ForecastMetrics()
        self._serving = artifact.config.serving

    @property
    def model_version(self) -> str:
        return self.artifact.model_version

    def forecast(
        self,
        series: Sequence[SeriesKey] | None = None,
        horizons: Sequence[int] | None = None,
        planned_prices: Mapping[str, int] | None = None,
    ) -> ForecastResult:
        started = time.perf_counter()
        labels: dict[str, Any] = {"model_version": self.model_version}
        try:
            result = self._forecast(series, horizons, planned_prices or {})
        except ForecastRequestError as exc:
            self._finish("rejected_request", started, labels | {"reason": exc.code})
            raise
        except ForecastUnavailable as exc:
            self._finish("unavailable", started, labels | {"reason": exc.code})
            raise
        self._finish(
            "served",
            started,
            labels
            | {
                "source": result.source.value,
                "freshness": result.freshness.value,
                "reason": result.fallback_reason,
                "feature_date": result.feature_date.isoformat(),
                "feature_lag_days": result.feature_lag_days,
                "points": len(result.points),
            },
        )
        return result

    def _finish(self, outcome: str, started: float, labels: dict[str, Any]) -> None:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.metrics.record(outcome, elapsed_ms, **labels)
        logger.info(
            "forecast.%s",
            outcome,
            extra={"forecast_outcome": outcome, "latency_ms": round(elapsed_ms, 3), **labels},
        )

    def _validate(
        self, series: Sequence[SeriesKey] | None, horizons: Sequence[int] | None
    ) -> tuple[list[SeriesKey], list[int]]:
        known = set(self.artifact.series)
        chosen = sorted(set(series)) if series else sorted(known)
        unknown = [s.label for s in chosen if s not in known]
        if unknown:
            raise ForecastRequestError("unknown_series", f"unknown series: {unknown[:5]}")
        if len(chosen) > self._serving.max_series_per_request:
            raise ForecastRequestError(
                "too_many_series", f"at most {self._serving.max_series_per_request} series"
            )
        allowed = self.artifact.config.target.horizons
        chosen_h = sorted(set(horizons)) if horizons else list(allowed)
        if any(h not in allowed for h in chosen_h):
            raise ForecastRequestError("invalid_horizon", f"horizons must be in {list(allowed)}")
        return chosen, chosen_h

    def _plan(self, snap: Snapshot, planned: Mapping[str, int]) -> PricePlan:
        if not planned:
            return snap.plan
        ratio = self._serving.max_planned_price_ratio
        for product, price in planned.items():
            if product not in self.artifact.catalogue.products:
                raise ForecastRequestError("unknown_product", f"unknown product {product}")
            current = snap.plan.price_on(product, snap.feature_date)
            if current is None:
                raise ForecastRequestError("unknown_product", f"no current price for {product}")
            if not current / ratio <= price <= current * ratio:
                raise ForecastRequestError(
                    "planned_price_out_of_range",
                    f"{product}: planned price must be within x{ratio} of {current}",
                )
        return snap.plan.with_planned(planned, snap.feature_date + timedelta(days=1))

    def _forecast(
        self,
        series: Sequence[SeriesKey] | None,
        horizons: Sequence[int] | None,
        planned: Mapping[str, int],
    ) -> ForecastResult:
        chosen, chosen_h = self._validate(series, horizons)
        now = self.clock()
        today = now.date()
        try:
            snap = self.source.snapshot(self.artifact.series, not_after=today)
        except WarehouseUnavailable as exc:
            raise ForecastUnavailable("warehouse_unavailable", str(exc)) from exc
        if snap is None:
            raise ForecastUnavailable("features_unavailable", "no complete feature day")
        lag = (today - snap.feature_date).days
        if lag > self._serving.stale_max_lag_days:
            raise ForecastUnavailable(
                "features_unavailable",
                f"latest feature day {snap.feature_date} is {lag} days old "
                f"(limit {self._serving.stale_max_lag_days})",
            )
        plan = self._plan(snap, planned)
        frame = build_features(
            snap.panel,
            snap.panel.n_days - 1,
            chosen_h,
            plan,
            self.artifact.catalogue,
            external=self.artifact.config.features.external,
        )
        wanted = set(chosen)
        frame = frame.take(
            np.array([snap.panel.series[i] in wanted for i in frame.series_idx.tolist()])
        )
        fresh = lag <= self._serving.fresh_max_lag_days
        pred: Prediction = (
            self.artifact.model.predict(frame) if fresh else self.artifact.fallback.predict(frame)
        )
        points = [
            ForecastPoint(
                series=snap.panel.series[int(si)],
                horizon_days=int(h),
                target_date=snap.feature_date + timedelta(days=int(h)),
                point=float(pred.point[r]),
                quantiles={lvl: float(pred.quantiles[r, j]) for j, lvl in enumerate(pred.levels)},
            )
            for r, (si, h) in enumerate(
                zip(frame.series_idx.tolist(), frame.horizon.tolist(), strict=True)
            )
        ]
        points.sort(key=lambda p: (p.series, p.horizon_days))
        cutoff = datetime.combine(snap.feature_date + timedelta(days=1), dtime(0), tzinfo=UTC)
        return ForecastResult(
            model_name=str(self.artifact.manifest["model_name"]),
            model_version=self.model_version,
            feature_version=str(self.artifact.manifest["feature_version"]),
            created_at=now,
            feature_date=snap.feature_date,
            feature_cutoff=cutoff,
            feature_lag_days=lag,
            freshness=Freshness.FRESH if fresh else Freshness.STALE,
            source=Source.MODEL if fresh else Source.FALLBACK,
            fallback_reason=None if fresh else "stale_features",
            quantile_levels=pred.levels,
            points=points,
        )
