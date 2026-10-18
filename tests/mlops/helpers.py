"""Small, fully known worlds for MLOps tests (SYNTHETIC).

One 168-day panel (12 series: 3 regions x 2 products x 2 segments; weekly seasonality,
diverging trends, a price cut, Gamma noise) written to a warehouse twice: as generated, and
with an injected level shift (region ``r_east`` x1.8 from day 126) for drift detection.

These fixtures test MECHANICS (gates reject, stages move, rollback restores), not model
quality, so two documented fixture settings differ from production:

* the forecast acceptance is scaled to a 12-series toy world (beat seasonal naive by 5%,
  wider coverage bands); the production gate is the pinned configs/forecast/demand.toml,
  exercised on the simulator's development world (docs/evidence/phase-10.md);
* the lifecycle policy's sample-size floors are scaled to 12 series instead of 72, and its
  coverage bands are widened: a 12-series toy's single final model covers 0.58-0.72 of
  its 80% intervals out of sample, which would make every toy test a calibration test.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from praxis.forecasting.config import ForecastConfig
from praxis.forecasting.panel import CONTEXT_COLUMNS, DemandPanel, SeriesKey
from praxis.mlops.config import MlopsConfig, load_mlops_config
from tests.forecasting.helpers import (
    PRICE_CHANGE_DAY,
    START,
    make_plan,
    small_config,
    write_warehouse,
)

N_DAYS = 168
TRAIN_END_DAY = 111  # champion: targets through day 111
SHIFT_DAY = 126
SHIFT_REGION = "r_east"
SHIFT = 1.8


REGIONS = ("r_east", "r_north", "r_west")
N_SERIES = 12


def panel(seed: int = 11, noise: float = 0.2, trend: float = 0.006) -> DemandPanel:
    rng = np.random.default_rng(seed)
    keys = tuple(
        sorted(
            SeriesKey(r, p, s)
            for r in REGIONS
            for p in ("p_api", "p_gpu")
            for s in ("s_big", "s_small")
        )
    )
    days = np.arange(N_DAYS)
    dow = (START.weekday() + days) % 7
    demand = np.zeros((len(keys), N_DAYS))
    for i, k in enumerate(keys):
        base = (400.0 if k.segment == "s_big" else 60.0) * (3.0 if k.product == "p_api" else 1.0)
        season = 1.0 + 0.3 * np.cos(2 * np.pi * (dow - (i % 7)) / 7)
        growth = np.exp(trend * days * (1.0 if i % 2 == 0 else -0.5))
        price = np.where((k.product == "p_api") & (days >= PRICE_CHANGE_DAY), 0.9, 1.0)
        demand[i] = np.round(rng.gamma(1 / noise**2, base * season * growth * price * noise**2))
    context = {c: rng.normal(1.0, 0.1, (len(REGIONS), N_DAYS)) for c in CONTEXT_COLUMNS}
    return DemandPanel(
        START, keys, REGIONS, demand, np.floor(demand * 0.97), np.full_like(demand, 5.0), context
    )


def shifted(p: DemandPanel) -> DemandPanel:
    """Unannounced, persistent level shift for one region (drift the model never saw)."""
    rows = np.array([s.region_id == SHIFT_REGION for s in p.series])
    demand, served = p.demand.copy(), p.served.copy()
    demand[rows, SHIFT_DAY:] = np.round(demand[rows, SHIFT_DAY:] * SHIFT)
    served[rows, SHIFT_DAY:] = np.floor(demand[rows, SHIFT_DAY:] * 0.97)
    return replace(p, demand=demand, served=served)


def throttled(p: DemandPanel, start: int = 120, share: float = 0.5) -> DemandPanel:
    """A capacity problem: only ``share`` of requested units served from ``start`` (an
    input-feature shift, served_ratio_7, that leaves the requested-demand target alone)."""
    served = p.served.copy()
    served[:, start:] = np.floor(p.demand[:, start:] * share)
    return replace(p, served=served)


def day(p: DemandPanel, i: int) -> date:
    return p.date_of(i)


def write(path: Path, p: DemandPanel) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_warehouse(path, p, make_plan())
    return path


FIXTURE_ACCEPTANCE = {
    "max_vwape_ratio_vs_seasonal_naive": 0.95,
    "point_must_beat": ["seasonal_naive"],
    "require_bootstrap_ci_below_zero": False,
    "pinball_must_beat": ["seasonal_naive"],
    "coverage_80": [0.6, 0.95],
    "coverage_50": [0.3, 0.7],
    "max_slice_vwape_ratio_vs_seasonal_naive": 1.2,
}


def forecast_config() -> ForecastConfig:
    return small_config(lightgbm={"num_boost_round": 40}, acceptance=FIXTURE_ACCEPTANCE)


def bad_forecast_config() -> ForecastConfig:
    """A plausible misconfiguration: ridge shrunk to a constant, a one-tree LightGBM."""
    return small_config(
        ridge={"alpha": 1e9}, lightgbm={"num_boost_round": 1}, acceptance=FIXTURE_ACCEPTANCE
    )


def mlops_config(**overrides: dict[str, Any]) -> MlopsConfig:
    raw = load_mlops_config().model_dump(mode="json")
    raw["monitoring"].update(min_rows=200, min_slice_rows=50, coverage_80=[0.5, 0.95])
    raw["shadow"].update(
        min_origins=7, min_slice_rows=50, coverage_80=[0.6, 0.95], coverage_50=[0.3, 0.7]
    )
    raw["retraining"].update(min_new_labels=N_SERIES * 21)
    for section, values in overrides.items():
        raw[section].update(values)
    return MlopsConfig.model_validate(raw)
