"""Dunning policy (validated, hashable): ``configs/recovery/policy.toml``.

One policy file drives the deterministic baseline, the model policy, the operational dunning
service and the science replay, so all four value a decision identically. Its content hash
is the ``policy_version`` stamped on every dunning decision.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_REPO = Path(__file__).resolve().parents[3]
DEFAULT_POLICY = _REPO / "configs" / "recovery" / "policy.toml"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Bounds(_Cfg):
    max_attempts: int = Field(ge=2, le=6)
    horizon_days: int = Field(ge=1, le=29)  # Cloud Tasks schedules at most 30 days ahead
    grace_days: int = Field(ge=0)
    max_job_lateness_hours: int = Field(ge=1)


class Baseline(_Cfg):
    retry_offsets_days: tuple[int, ...]


class Valuation(_Cfg):
    retry_cost_minor: int = Field(ge=0)
    failed_retry_cost_minor: int = Field(ge=0)
    delay_cost_per_day: float = Field(ge=0, lt=0.05)


class ModelSettings(_Cfg):
    max_age_days: int = Field(ge=1)
    feature_version: str = Field(min_length=1)


class RecoveryPolicy(_Cfg):
    bounds: Bounds
    baseline: Baseline
    valuation: Valuation
    model: ModelSettings

    @model_validator(mode="after")
    def _check(self) -> Self:
        offsets = self.baseline.retry_offsets_days
        if len(offsets) != self.bounds.max_attempts - 1 or any(o < 1 for o in offsets):
            raise ValueError("baseline needs max_attempts - 1 positive retry offsets")
        if sum(offsets) > self.bounds.horizon_days:
            raise ValueError("the baseline schedule must fit inside horizon_days")
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def version(self) -> str:
        return f"dunning-{hashlib.sha256(self.canonical_json().encode()).hexdigest()[:12]}"

    @property
    def baseline_schedule(self) -> tuple[int, ...]:
        """Elapsed days (from the first failure) of every baseline retry."""
        out, total = [], 0
        for offset in self.baseline.retry_offsets_days:
            total += offset
            out.append(total)
        return tuple(out)


def load_policy(path: Path | None = None) -> RecoveryPolicy:
    return RecoveryPolicy.model_validate(
        tomllib.loads((path or DEFAULT_POLICY).read_text(encoding="utf-8"))
    )


DEFAULT_MODEL_CONFIG = _REPO / "configs" / "recovery" / "model.toml"


class ClassifierConfig(_Cfg):
    learning_rate: float = Field(gt=0, le=1)
    num_boost_round: int = Field(ge=1)
    num_leaves: int = Field(ge=2)
    min_data_in_leaf: int = Field(ge=1)
    lambda_l2: float = Field(ge=0)
    seed: int
    calibration_fraction: float = Field(gt=0, lt=1)


class SurvivalConfig(_Cfg):
    ridge: float = Field(ge=0)
    max_iter: int = Field(ge=10)
    gtol: float = Field(gt=0)


class ModelConfig(_Cfg):
    classifier: ClassifierConfig
    survival: SurvivalConfig

    @property
    def config_hash(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


def load_model_config(path: Path | None = None) -> ModelConfig:
    return ModelConfig.model_validate(
        tomllib.loads((path or DEFAULT_MODEL_CONFIG).read_text(encoding="utf-8"))
    )
