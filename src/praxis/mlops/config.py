"""Lifecycle policy (validated). ``configs/mlops/demand.toml`` is pre-registered and pinned."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from praxis.config import Environment
from praxis.domain.model_lifecycle import PROMOTION_PATH, ModelStage

DEFAULT_MLOPS_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "mlops" / "demand.toml"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _band(lo_hi: tuple[float, float]) -> tuple[float, float]:
    lo, hi = lo_hi
    if not 0.0 <= lo < hi <= 1.0:
        raise ValueError(f"invalid coverage band {lo_hi}")
    return lo_hi


class Lifecycle(_Cfg):
    stages: tuple[ModelStage, ...]
    auto_advance_to: ModelStage
    champion_requires_approval: bool
    forced_promotion_environments: tuple[Environment, ...] = ()

    @model_validator(mode="after")
    def _path(self) -> Self:
        order = [s for s in PROMOTION_PATH if s in self.stages]
        if list(self.stages) != order or not self.stages or self.stages[-1] != "champion":
            raise ValueError(f"stages must be an ordered subsequence of {PROMOTION_PATH}")
        if ModelStage.CANARY in self.stages:
            raise ValueError("canary is not supported: no canary evaluation exists (ADR 0017)")
        if self.stages[0] != ModelStage.CHALLENGER:
            raise ValueError("every candidate must pass validation into challenger first")
        if self.auto_advance_to not in self.stages:
            raise ValueError("auto_advance_to must be one of the stages")
        if self.auto_advance_to == ModelStage.CHAMPION and self.champion_requires_approval:
            raise ValueError("auto_advance_to=champion contradicts champion_requires_approval")
        if Environment.PROD in self.forced_promotion_environments:
            raise ValueError("forced promotion is never allowed in prod")
        return self

    def next_stage(self, stage: ModelStage) -> ModelStage | None:
        """The configured stage after ``stage`` (``None`` at champion or off-path)."""
        if stage not in self.stages:
            return None
        i = self.stages.index(stage)
        return self.stages[i + 1] if i + 1 < len(self.stages) else None

    def automatic(self, stage: ModelStage) -> bool:
        """May automation move a version INTO ``stage`` without a named approver?"""
        return stage in self.stages and self.stages.index(stage) <= self.stages.index(
            self.auto_advance_to
        )


class Validation(_Cfg):
    require_backtest_acceptance: bool
    require_clean_code_revision: bool
    min_training_days: int = Field(ge=28)
    min_training_rows: int = Field(ge=1)
    latency_repeats: int = Field(ge=3)
    latency_budget_ms_p95: float = Field(gt=0)
    memory_budget_mb: float = Field(gt=0)
    max_artifact_mb: float = Field(gt=0)
    leakage_poison_factor: float = Field(gt=1)
    max_skill_ratio_vs_champion: float = Field(gt=0)


class Shadow(_Cfg):
    min_origins: int = Field(ge=1)
    max_vwape_ratio_vs_champion: float = Field(gt=0)
    max_pinball_ratio_vs_champion: float = Field(gt=0)
    max_slice_vwape_ratio_vs_champion: float = Field(gt=0)
    min_slice_rows: int = Field(ge=1)
    coverage_80: tuple[float, float]
    coverage_50: tuple[float, float]

    @model_validator(mode="after")
    def _bands(self) -> Self:
        _band(self.coverage_80)
        _band(self.coverage_50)
        return self


class Monitoring(_Cfg):
    window_days: int = Field(ge=7)
    min_rows: int = Field(ge=1)
    max_vwape_ratio_vs_reference: float = Field(gt=1)
    max_slice_vwape_ratio_vs_reference: float = Field(gt=1)
    min_slice_rows: int = Field(ge=1)
    coverage_80: tuple[float, float]
    psi_alert: float = Field(gt=0)
    psi_alpha: float = Field(gt=0, lt=0.5)
    psi_bins: int = Field(ge=4, le=50)
    reference_days: int = Field(ge=7)
    features: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _bands(self) -> Self:
        _band(self.coverage_80)
        return self


class Retraining(_Cfg):
    scheduled_every_days: int = Field(ge=1)
    min_new_labels: int = Field(ge=1)
    cooldown_days: int = Field(ge=0)


class Probation(_Cfg):
    days: int = Field(ge=1)
    min_origins: int = Field(ge=1)
    max_vwape_ratio_vs_previous: float = Field(ge=1)
    auto_rollback: bool


class MlopsConfig(_Cfg):
    model_name: str = Field(min_length=1)
    lifecycle: Lifecycle
    validation: Validation
    shadow: Shadow
    monitoring: Monitoring
    retraining: Retraining
    probation: Probation

    @property
    def policy_version(self) -> str:
        """Hash of the validated policy (recorded on every lifecycle decision)."""
        text = self.model_dump_json()
        return f"mlops-{hashlib.sha256(text.encode()).hexdigest()[:12]}"


def load_mlops_config(path: Path | None = None) -> MlopsConfig:
    raw = tomllib.loads((path or DEFAULT_MLOPS_CONFIG).read_text(encoding="utf-8"))
    return MlopsConfig.model_validate(raw)
