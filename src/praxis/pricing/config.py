"""Pricing policy (validated, hashable): ``configs/pricing/policy.toml``.

The policy is the set of business rules the optimiser obeys. Its content hash is the
``policy_version`` stamped on every decision, so a decision can always be traced to the exact
rules that produced it.
"""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_REPO = Path(__file__).resolve().parents[3]
DEFAULT_POLICY = _REPO / "configs" / "pricing" / "policy.toml"


class Mode(StrEnum):
    SHADOW = "shadow"  # record only; never executable
    RECOMMEND = "recommend"  # record; executable only after a recorded human approval
    EXECUTE = "execute"  # record; executable once the record is persisted


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class General(_Cfg):
    name: str = Field(min_length=1)
    mode: Mode
    allow_execute: bool
    horizon_days: int = Field(ge=1, le=7)
    price_tick_micros: int = Field(ge=1)
    grid_points: int = Field(ge=3, le=401)


class Constraints(_Cfg):
    max_step: float = Field(gt=0, lt=1)
    cooldown_days: int = Field(ge=0)
    min_contribution_margin: float = Field(ge=0, lt=1)
    max_utilization: float = Field(gt=0, le=1.5)
    capacity_quantile: float = Field(gt=0.5, lt=1)
    max_incremental_churn: float = Field(ge=0, lt=1)


class Uncertainty(_Cfg):
    max_elasticity_sd: float = Field(gt=0)
    material_tier_share: float = Field(ge=0, lt=1)
    min_prob_improvement: float = Field(ge=0.5, lt=1)
    risk_aversion: float = Field(ge=0)
    max_forecast_relative_width: float = Field(gt=0)
    z_grid: int = Field(ge=11, le=2001)


class Evidence(_Cfg):
    max_extrapolation: float = Field(gt=0)
    max_evidence_age_days: int = Field(ge=1)
    churn_upper_z: float = Field(ge=0)
    require_fresh_forecast: bool


class Valuation(_Cfg):
    payment_loss_lookback_days: int = Field(ge=1)
    cost_lookback_days: int = Field(ge=1)
    utilization_lookback_days: int = Field(ge=1)
    customer_lookback_days: int = Field(ge=1)
    clv_lifetime_cap_days: int = Field(ge=1)


class ProductBounds(_Cfg):
    floor_micros: int = Field(gt=0)
    ceiling_micros: int = Field(gt=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.ceiling_micros <= self.floor_micros:
            raise ValueError("ceiling_micros must exceed floor_micros")
        return self


class PricingPolicy(_Cfg):
    policy: General
    constraints: Constraints
    uncertainty: Uncertainty
    evidence: Evidence
    valuation: Valuation
    products: dict[str, ProductBounds] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.policy.mode is Mode.EXECUTE and not self.policy.allow_execute:
            raise ValueError("mode = execute needs allow_execute = true")
        if self.policy.grid_points % 2 == 0:
            raise ValueError("grid_points must be odd (the grid is symmetric around the price)")
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    @property
    def version(self) -> str:
        return f"policy-{self.config_hash[:12]}"

    @property
    def max_log_step(self) -> float:
        return math.log1p(self.constraints.max_step)

    def with_mode(self, mode: Mode, *, allow_execute: bool | None = None) -> PricingPolicy:
        """Same rules, another mode (the mode is part of the version)."""
        general = self.policy.model_copy(
            update={
                "mode": mode,
                "allow_execute": self.policy.allow_execute
                if allow_execute is None
                else allow_execute,
            }
        )
        return PricingPolicy.model_validate({**self.model_dump(), "policy": general.model_dump()})


def load_policy(path: Path | None = None) -> PricingPolicy:
    return PricingPolicy.model_validate(
        tomllib.loads((path or DEFAULT_POLICY).read_text(encoding="utf-8"))
    )
