"""Forecast configuration (validated). ``configs/forecast/demand.toml`` is the stable file.

The ``acceptance`` table is pre-registered: thresholds are fixed before any backtest runs
(ADR 0010) and must not be loosened after a result is seen.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_FORECAST_CONFIG = (
    Path(__file__).resolve().parents[3] / "configs" / "forecast" / "demand.toml"
)


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Target(_Cfg):
    horizons: tuple[int, ...]
    quantiles: tuple[float, ...]
    min_history_days: int = Field(ge=28)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.horizons or list(self.horizons) != sorted(set(self.horizons)):
            raise ValueError("horizons must be strictly increasing")
        if self.horizons[0] < 1 or self.horizons[-1] > 7:
            raise ValueError("horizons must lie in 1..7 (same-weekday lags need D-7 <= T)")
        qs = list(self.quantiles)
        if qs != sorted(set(qs)) or not all(0.0 < q < 1.0 for q in qs):
            raise ValueError("quantiles must be strictly increasing in (0, 1)")
        for lo, hi in ((0.1, 0.9), (0.25, 0.75)):
            if lo not in qs or hi not in qs:
                raise ValueError("quantiles must include 0.1/0.9 and 0.25/0.75 (coverage)")
        return self


class Features(_Cfg):
    external: bool = False


class LightGBMParams(_Cfg):
    num_boost_round: int = Field(gt=0)
    learning_rate: float = Field(gt=0, le=1)
    num_leaves: int = Field(ge=2)
    min_data_in_leaf: int = Field(ge=1)
    feature_fraction: float = Field(gt=0, le=1)
    lambda_l2: float = Field(ge=0)
    seed: int
    num_threads: int = Field(ge=1)
    calibration_days: int = Field(ge=0)


class Ridge(_Cfg):
    alpha: float = Field(gt=0)


class Backtest(_Cfg):
    initial_train_days: int = Field(ge=35)
    step_days: int = Field(ge=1)
    bootstrap_samples: int = Field(ge=100)
    bootstrap_seed: int


class Evaluation(_Cfg):
    value_weights: dict[str, float]

    @model_validator(mode="after")
    def _positive(self) -> Self:
        if not self.value_weights or any(w <= 0 for w in self.value_weights.values()):
            raise ValueError("value_weights must be positive")
        return self


class Serving(_Cfg):
    fresh_max_lag_days: int = Field(ge=0)
    stale_max_lag_days: int = Field(ge=0)
    max_planned_price_ratio: float = Field(gt=1)
    max_series_per_request: int = Field(ge=1)
    latency_budget_ms_p95: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.stale_max_lag_days < self.fresh_max_lag_days:
            raise ValueError("stale_max_lag_days must be >= fresh_max_lag_days")
        return self


class Acceptance(_Cfg):
    candidate: str
    max_vwape_ratio_vs_seasonal_naive: float = Field(gt=0)
    point_must_beat: tuple[str, ...]
    require_bootstrap_ci_below_zero: bool
    max_pinball_ratio_vs_seasonal_naive: float = Field(gt=0)
    pinball_must_beat: tuple[str, ...]
    coverage_80: tuple[float, float]
    coverage_50: tuple[float, float]
    max_slice_vwape_ratio_vs_seasonal_naive: float = Field(gt=0)
    reproducibility_tolerance: float = Field(ge=0)


class ForecastConfig(_Cfg):
    target: Target
    features: Features = Features()
    lightgbm: LightGBMParams
    ridge: Ridge
    backtest: Backtest
    evaluation: Evaluation
    serving: Serving
    acceptance: Acceptance

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def with_external(self, external: bool) -> Self:
        return self.model_copy(update={"features": Features(external=external)})


def load_forecast_config(path: Path | None = None) -> ForecastConfig:
    raw = tomllib.loads((path or DEFAULT_FORECAST_CONFIG).read_text(encoding="utf-8"))
    return ForecastConfig.model_validate(raw)
