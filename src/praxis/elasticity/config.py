"""Elasticity analysis configuration and the experiment registry (validated, hashable).

``configs/elasticity/elasticity.toml`` holds the pre-registered analysis rules and the truth-free
gates (experiment validity, Bayesian diagnostics). ``configs/experiments/*.toml`` is the
business-side record of each randomised price test. Ground-truth acceptance is NOT here: it
lives in ``configs/elasticity/acceptance.toml`` and is read only by ``praxis.science``.
"""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_REPO = Path(__file__).resolve().parents[3]
DEFAULT_ELASTICITY_CONFIG = _REPO / "configs" / "elasticity" / "elasticity.toml"
DEFAULT_REGISTRY = _REPO / "configs" / "experiments" / "elasticity_eval.toml"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


# ------------------------------------------------------------------ experiment registry
class ExperimentDesign(_Cfg):
    id: str = Field(min_length=1)
    product: str = Field(min_length=1)
    assignment_unit: Literal["customer"]
    salt: str = Field(min_length=1)
    treated_fraction: float = Field(gt=0, lt=1)
    treatment_price_ratio: float = Field(gt=0)
    assignment_date: date
    end_date: date

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.end_date < self.assignment_date:
            raise ValueError(f"experiment {self.id}: end_date before assignment_date")
        if self.treatment_price_ratio == 1.0:
            raise ValueError(f"experiment {self.id}: treatment price equals control price")
        return self

    @property
    def log_ratio(self) -> float:
        return math.log(self.treatment_price_ratio)

    @property
    def window_days(self) -> int:
        return (self.end_date - self.assignment_date).days + 1

    def pre_start(self, pre_window_days: int) -> date:
        return self.assignment_date - timedelta(days=pre_window_days)


class ExperimentRegistry(_Cfg):
    experiments: tuple[ExperimentDesign, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        ids = [e.id for e in self.experiments]
        salts = [e.salt for e in self.experiments]
        if len(set(ids)) != len(ids) or len(set(salts)) != len(salts):
            raise ValueError("experiment ids and salts must be unique")
        for i, a in enumerate(self.experiments):
            for b in self.experiments[i + 1 :]:
                overlap = a.assignment_date <= b.end_date and b.assignment_date <= a.end_date
                if overlap and a.product == b.product:
                    raise ValueError("overlapping experiments on one product are not allowed")
        return self


# ------------------------------------------------------------------ analysis configuration
class Eligibility(_Cfg):
    pre_window_days: int = Field(ge=7)
    min_pre_units: int = Field(ge=0)
    require_created_by_pre_start: bool


class Outcome(_Cfg):
    min_active_days: int = Field(ge=1)
    log_offset: float = Field(gt=0)


class Estimation(_Cfg):
    ci_level: float = Field(gt=0, lt=1)
    min_units_per_cell: int = Field(ge=2)


class Validity(_Cfg):
    srm_alpha: float = Field(gt=0, lt=1)
    balance_alpha: float = Field(gt=0, lt=1)
    smd_flag: float = Field(gt=0)
    max_missing_rate: float = Field(ge=0, le=1)
    max_missing_rate_diff: float = Field(ge=0, le=1)
    max_contamination_rate: float = Field(ge=0, le=1)
    price_tolerance_micros: int = Field(ge=0)
    interference_alpha: float = Field(gt=0, lt=1)


class Priors(_Cfg):
    mu_mean: float
    mu_sd: float = Field(gt=0)
    tier_sd: float = Field(gt=0)
    industry_sd: float = Field(gt=0)
    cell_sd: float = Field(gt=0)


class Hierarchical(_Cfg):
    draws: int = Field(ge=100)
    tune: int = Field(ge=100)
    chains: int = Field(ge=2)
    target_accept: float = Field(gt=0.5, lt=1)
    seed: int
    interval: float = Field(gt=0, lt=1)
    priors: Priors
    sensitivity: dict[str, Priors] = Field(min_length=1)


class Diagnostics(_Cfg):
    rhat_max: float = Field(gt=1)
    ess_bulk_min: float = Field(gt=0)
    ess_tail_min: float = Field(gt=0)
    max_divergences: int = Field(ge=0)
    ppc_p_range: tuple[float, float]
    prior_sensitivity_max_shift_sd: float = Field(gt=0)

    @model_validator(mode="after")
    def _range(self) -> Self:
        lo, hi = self.ppc_p_range
        if not 0.0 <= lo < hi <= 1.0:
            raise ValueError("ppc_p_range must satisfy 0 <= lo < hi <= 1")
        return self


class ElasticityConfig(_Cfg):
    eligibility: Eligibility
    outcome: Outcome
    estimation: Estimation
    validity: Validity
    hierarchical: Hierarchical
    diagnostics: Diagnostics

    def with_sampler(self, *, draws: int, tune: int) -> Self:
        """Smaller sampler budget (tests only); everything else unchanged."""
        h = self.hierarchical.model_copy(update={"draws": draws, "tune": tune})
        return self.model_copy(update={"hierarchical": h})


def load_elasticity_config(path: Path | None = None) -> ElasticityConfig:
    raw = tomllib.loads((path or DEFAULT_ELASTICITY_CONFIG).read_text(encoding="utf-8"))
    return ElasticityConfig.model_validate(raw)


def load_registry(path: Path | None = None) -> ExperimentRegistry:
    raw = tomllib.loads((path or DEFAULT_REGISTRY).read_text(encoding="utf-8"))
    return ExperimentRegistry.model_validate(raw)
