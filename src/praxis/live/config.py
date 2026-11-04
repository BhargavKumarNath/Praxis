"""The live loop's pre-registered configuration (``configs/live/live.toml``, ADR 0027)."""

from __future__ import annotations

import tomllib
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from praxis.simulator.config import load_config

REPO = Path(__file__).resolve().parents[3]
DEFAULT_LIVE_CONFIG = REPO / "configs" / "live" / "live.toml"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WorldConfig(_Frozen):
    scenario: Path
    seed: int = Field(ge=0)
    customers: int = Field(gt=0)
    start_date: date

    @property
    def scenario_path(self) -> Path:
        return self.scenario if self.scenario.is_absolute() else REPO / self.scenario


class BootstrapConfig(_Frozen):
    train_through: date


class TickConfig(_Frozen):
    max_catchup_days: int = Field(ge=1, le=31)
    review_ready_shadow: bool


class LiveConfig(_Frozen):
    world: WorldConfig
    bootstrap: BootstrapConfig
    tick: TickConfig

    @model_validator(mode="after")
    def _consistent(self) -> LiveConfig:
        if self.bootstrap.train_through <= self.world.start_date:
            raise ValueError("bootstrap.train_through must be after world.start_date")
        return self


def load_live_config(path: Path | None = None) -> LiveConfig:
    cfg = LiveConfig.model_validate(tomllib.loads((path or DEFAULT_LIVE_CONFIG).read_text()))
    scenario_start = load_config(scenario=cfg.world.scenario_path).run.start_date
    if scenario_start != cfg.world.start_date:
        raise ValueError(
            f"world.start_date {cfg.world.start_date} differs from the scenario's {scenario_start}"
        )
    return cfg
