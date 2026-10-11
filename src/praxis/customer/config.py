"""Customer-value configuration (validated, hashable): ``configs/customer/value.toml``.

The file is pre-registered (ADR 0016); its hash is recorded in every customer artifact, so a
model can always be traced to the exact label, feature and valuation rules that produced it.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_REPO = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = _REPO / "configs" / "customer" / "value.toml"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Label(_Cfg):
    window_days: int = Field(ge=1, le=365)
    snapshot_step_days: int = Field(ge=1)
    first_snapshot_day: int = Field(ge=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.snapshot_step_days < self.window_days:
            raise ValueError("snapshot windows must not overlap (step >= window)")
        return self


class Features(_Cfg):
    spend_window_days: int = Field(ge=1)
    trend_half_days: int = Field(ge=1)
    failure_window_days: int = Field(ge=1)
    service_window_days: int = Field(ge=1)


class Hazard(_Cfg):
    ridge: float = Field(ge=0)
    max_iter: int = Field(ge=1)
    tol: float = Field(gt=0)
    backtest_windows: int = Field(ge=1)
    rate_prior_weight: float = Field(ge=0)


class Calibration(_Cfg):
    enabled: bool
    # "level": the last label window's observed / expected (ADR 0016);
    # "trend": Poisson trend over the last `weeks` weekly windows, forecast to the middle of
    # the decision week (ADR 0016 second amendment: a level nowcast lags a rising hazard)
    method: Literal["level", "trend"] = "level"
    weeks: int = Field(default=8, ge=2)
    horizon_days: float = Field(default=3.5, ge=0)
    min_events: int = Field(default=10, ge=1)  # fewer observed churners: fall back to "level"
    slope_prior_sd: float = Field(default=0.1, gt=0)  # N(0, sd^2) prior on the weekly log trend


class Clv(_Cfg):
    horizon_days: int = Field(ge=1)
    annual_discount_rate: float = Field(ge=0, lt=1)
    sensitivity_discount_rates: tuple[float, ...] = Field(min_length=1)
    sensitivity_horizons_days: tuple[int, ...] = Field(min_length=1)


class Segments(_Cfg):
    names: tuple[str, ...] = Field(min_length=1)


class Monitoring(_Cfg):
    max_window_churn: float = Field(gt=0, le=1)
    max_calibration_ratio: float = Field(gt=1)
    min_customers: int = Field(ge=1)


class CustomerValueConfig(_Cfg):
    label: Label
    features: Features
    hazard: Hazard
    calibration: Calibration
    clv: Clv
    segments: Segments
    monitoring: Monitoring

    @model_validator(mode="after")
    def _check(self) -> Self:
        f = self.features
        history = max(f.spend_window_days, 2 * f.trend_half_days, f.failure_window_days)
        if self.label.first_snapshot_day < history:
            raise ValueError("first_snapshot_day must leave a full feature history")
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


def load_config(path: Path | None = None) -> CustomerValueConfig:
    return CustomerValueConfig.model_validate(
        tomllib.loads((path or DEFAULT_CONFIG).read_text(encoding="utf-8"))
    )
