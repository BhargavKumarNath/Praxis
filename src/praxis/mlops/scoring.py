"""Point-in-time scoring of demand-forecast artifacts on matured observations.

Shadow evaluation, drift monitoring and probation all ask the same question: what did a
version forecast at origin ``o`` (features from days <= o only) and what happened? The
frame is built ONCE and scored by every version being compared, so champion and challenger
see exactly the same eligible observations.

Production forecasts are re-scored from the warehouse rather than read from a log of served
forecasts (none exists before Phase 11); with the simulator's on-time data this equals what
the service would have served at each origin.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from praxis.forecasting.artifact import ForecastArtifact
from praxis.forecasting.features import WINDOW, FeatureFrame, actual, build_frame
from praxis.forecasting.metrics import sliced, summarize
from praxis.forecasting.models import Prediction
from praxis.forecasting.panel import DemandPanel, PricePlan, SeriesKey
from praxis.forecasting.warehouse import connect, load_panel, load_price_plan, usage_date_range

F64 = NDArray[np.float64]
SLICE_GROUPS = ("region", "product", "segment")


class IncompatibleVersions(ValueError):
    """Two artifacts cannot be scored on one frame (catalogue / horizons / features differ)."""


@dataclass(frozen=True)
class Observations:
    """Matured (series, origin, horizon) rows: features at the origin, the realised target."""

    panel: DemandPanel
    frame: FeatureFrame
    y: F64
    weight: F64

    def __len__(self) -> int:
        return len(self.y)

    def groups(self) -> dict[str, NDArray[Any]]:
        s = self.panel.series
        idx = self.frame.series_idx.tolist()
        return {
            "region": np.array([s[i].region_id for i in idx]),
            "product": np.array([s[i].product for i in idx]),
            "segment": np.array([s[i].segment for i in idx]),
        }

    @property
    def origins(self) -> list[int]:
        return sorted({int(o) for o in self.frame.origin.tolist()})


@dataclass(frozen=True)
class WarehouseData:
    panel: DemandPanel
    plan: PricePlan


def load_data(
    warehouse: Path,
    *,
    end: date,
    series: Sequence[SeriesKey] | None = None,
    start: date | None = None,
) -> WarehouseData:
    """Panel for ``start..end`` (default: the warehouse's first day; every observed series
    unless ``series`` is given) and the list-price plan known by ``end``."""
    con = connect(warehouse)
    try:
        rng = usage_date_range(con)
        if rng is None:
            raise ValueError("warehouse has no usage data")
        first = start or rng[0]
        last = min(end, rng[1])
        return WarehouseData(
            load_panel(con, first, last, series=series), load_price_plan(con, until=last)
        )
    finally:
        con.close()


def check_compatible(artifacts: Sequence[ForecastArtifact]) -> None:
    first = artifacts[0]
    for other in artifacts[1:]:
        if other.catalogue != first.catalogue or other.series != first.series:
            raise IncompatibleVersions(f"{other.model_version}: series catalogue differs")
        if other.config.target.horizons != first.config.target.horizons:
            raise IncompatibleVersions(f"{other.model_version}: horizons differ")
        if other.config.target.quantiles != first.config.target.quantiles:
            raise IncompatibleVersions(f"{other.model_version}: quantile levels differ")
        if other.manifest["feature_version"] != first.manifest["feature_version"]:
            raise IncompatibleVersions(f"{other.model_version}: feature version differs")
        if other.config.features.external != first.config.features.external:
            raise IncompatibleVersions(f"{other.model_version}: feature set differs")


def observations(
    artifact: ForecastArtifact,
    data: WarehouseData,
    origins: Sequence[int],
    *,
    target_window: tuple[int, int] | None = None,
) -> Observations | None:
    """Rows for ``origins`` whose target is observed (and inside ``target_window`` if given)."""
    panel = data.panel
    usable = [o for o in origins if WINDOW - 1 <= o < panel.n_days - 1]
    if not usable:
        return None
    frame = build_frame(
        panel,
        usable,
        artifact.config.target.horizons,
        data.plan,
        artifact.catalogue,
        external=artifact.config.features.external,
    )
    y = actual(panel, frame)
    keep = ~np.isnan(y)
    if target_window is not None:
        lo, hi = target_window
        keep &= (frame.target_day >= lo) & (frame.target_day <= hi)
    if not keep.any():
        return None
    frame = frame.take(keep)
    weights = artifact.config.evaluation.value_weights
    per_series = np.array([weights[s.product] for s in panel.series])
    return Observations(panel, frame, y[keep], per_series[frame.series_idx])


def predict(artifact: ForecastArtifact, obs: Observations) -> Prediction:
    return artifact.model.predict(obs.frame)


def metrics(obs: Observations, pred: Prediction) -> dict[str, Any]:
    """Pooled metrics + region / product / segment slices (same definitions as Phase 4)."""
    return {
        "overall": summarize(obs.y, pred, obs.weight),
        "slices": sliced(obs.y, pred, obs.weight, obs.groups()),
    }


def training_end(artifact: ForecastArtifact) -> date:
    """Last target date the artifact was trained on (its out-of-sample boundary)."""
    return date.fromisoformat(str(artifact.manifest["training"]["last_target_date"]))


def matured_origins(
    panel: DemandPanel, *, first: date, as_of: date, max_horizon: int, full: bool
) -> list[int]:
    """Origins from ``first`` whose targets are observed by ``as_of - 1``.

    ``full``: every horizon matured (shadow scoring); otherwise any horizon (monitoring).
    """
    last_observed = panel.day_of(as_of - timedelta(days=1))
    last_origin = last_observed - (max_horizon if full else 1)
    start = max(panel.day_of(first), WINDOW - 1)
    return list(range(start, min(last_origin, panel.n_days - 2) + 1))


def worst_slice_ratio(
    numerator: Mapping[str, Any], denominator: Mapping[str, Any], min_rows: int
) -> tuple[float, str]:
    """Largest slice vWAPE ratio over slices with at least ``min_rows`` rows in both."""
    worst, label = 0.0, ""
    for group in SLICE_GROUPS:
        for name, res in numerator.get(group, {}).items():
            ref = denominator.get(group, {}).get(name)
            if ref is None or res["rows"] < min_rows or not ref["vwape"] > 0:
                continue
            ratio = float(res["vwape"]) / float(ref["vwape"])
            if ratio > worst:
                worst, label = ratio, f"{group}={name}"
    return worst, label
