"""Dense daily demand panel plus the price plan: the forecaster's whole view of the world.

``DemandPanel`` holds *observed outcomes* per series and day, and regional context per
region and day. ``PricePlan`` holds list prices the business has set or scheduled; it is
the only input that may legitimately describe the future (ADR 0010).
"""

from __future__ import annotations

import bisect
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
from numpy.typing import NDArray

F64 = NDArray[np.float64]
I64 = NDArray[np.int64]

# Regional service context (simulated) and real external signals, from feat_region_daily.
SERVICE_CONTEXT = ("avg_utilization", "max_utilization", "avg_error_rate", "avg_latency_p95_ms")
EXTERNAL_CONTEXT = (
    "temperature_c_mean",
    "carbon_intensity_gco2_kwh_mean",
    "grid_demand_mwh_mean",
    "macro_cpiaucsl",
)
CONTEXT_COLUMNS = SERVICE_CONTEXT + EXTERNAL_CONTEXT


@dataclass(frozen=True, order=True)
class SeriesKey:
    region_id: str
    product: str
    segment: str

    @property
    def label(self) -> str:
        return f"{self.region_id}/{self.product}/{self.segment}"


@dataclass(frozen=True)
class DemandPanel:
    """Outcomes for days ``start_date .. start_date + n_days - 1``. Arrays are (S, N) / (R, N)."""

    start_date: date
    series: tuple[SeriesKey, ...]
    regions: tuple[str, ...]
    demand: F64  # requested units (served + throttled)
    served: F64
    active: F64  # customers with usage that day
    context: Mapping[str, F64] = field(default_factory=dict)  # NaN = not observed

    def __post_init__(self) -> None:
        shape = (len(self.series), self.n_days)
        for name in ("served", "active"):
            if getattr(self, name).shape != shape:
                raise ValueError(f"{name} must have shape {shape}")
        if list(self.series) != sorted(set(self.series)):
            raise ValueError("series must be unique and sorted")
        if any(s.region_id not in self.regions for s in self.series):
            raise ValueError("every series region must be in regions")
        for name, arr in self.context.items():
            if arr.shape != (len(self.regions), self.n_days):
                raise ValueError(f"context {name} must have shape (regions, days)")
        if np.any(self.demand < 0) or np.any(self.served > self.demand + 1e-9):
            raise ValueError("demand must be >= served >= 0")

    @property
    def n_days(self) -> int:
        return int(self.demand.shape[1])

    @property
    def end_date(self) -> date:
        return self.date_of(self.n_days - 1)

    def date_of(self, day: int) -> date:
        return self.start_date + timedelta(days=day)

    def day_of(self, d: date) -> int:
        return (d - self.start_date).days

    def series_region_index(self) -> I64:
        lookup = {r: i for i, r in enumerate(self.regions)}
        return np.array([lookup[s.region_id] for s in self.series], dtype=np.int64)

    def history(self, origin: int) -> DemandPanel:
        """Days ``<= origin`` only: the information available at the end of day ``origin``."""
        if not 0 <= origin < self.n_days:
            raise IndexError(f"origin {origin} outside panel of {self.n_days} days")
        end = origin + 1
        return DemandPanel(
            start_date=self.start_date,
            series=self.series,
            regions=self.regions,
            demand=self.demand[:, :end].copy(),
            served=self.served[:, :end].copy(),
            active=self.active[:, :end].copy(),
            context={k: v[:, :end].copy() for k, v in self.context.items()},
        )

    def data_version(self) -> str:
        h = hashlib.sha256()
        h.update(self.start_date.isoformat().encode())
        h.update("|".join(s.label for s in self.series).encode())
        h.update("|".join(self.regions).encode())
        for arr in (self.demand, self.served, self.active):
            h.update(np.ascontiguousarray(arr, dtype=np.float64).tobytes())
        for name in sorted(self.context):
            h.update(name.encode())
            h.update(np.ascontiguousarray(self.context[name], dtype=np.float64).tobytes())
        return "panel-" + h.hexdigest()[:16]


@dataclass(frozen=True)
class PricePlan:
    """List price per product over time: ``(effective_date, list_price_micros)`` change points.

    Before its first change point a product has no known price (``None``).
    """

    changes: Mapping[str, tuple[tuple[date, int], ...]]

    def __post_init__(self) -> None:
        for product, points in self.changes.items():
            days = [d for d, _ in points]
            if days != sorted(set(days)) or any(p <= 0 for _, p in points):
                raise ValueError(f"price plan for {product} must be dated, unique, positive")

    def price_on(self, product: str, d: date) -> int | None:
        points = self.changes.get(product, ())
        i = bisect.bisect_right([p[0] for p in points], d)
        return points[i - 1][1] if i else None

    def with_planned(self, planned: Mapping[str, int], effective: date) -> PricePlan:
        """Plan where ``planned`` prices apply from ``effective`` onwards (later points dropped)."""
        out = {p: tuple(pt for pt in pts) for p, pts in self.changes.items()}
        for product, price in planned.items():
            kept = tuple(pt for pt in out.get(product, ()) if pt[0] < effective)
            out[product] = (*kept, (effective, int(price)))
        return PricePlan(out)

    def version(self) -> str:
        h = hashlib.sha256()
        for product in sorted(self.changes):
            for d, price in self.changes[product]:
                h.update(f"{product}:{d.isoformat()}:{price};".encode())
        return "prices-" + h.hexdigest()[:16]
