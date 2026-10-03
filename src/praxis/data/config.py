"""Loader for ``configs/data/sources.toml``."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from praxis.data.models import SourceId


def default_config_path() -> Path:
    return Path(__file__).resolve().parents[3] / "configs" / "data" / "sources.toml"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Location(_Frozen):
    region_id: str
    name: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    carbon_area: str | None = None
    eia_respondent: str | None = None


class OpenMeteoConfig(_Frozen):
    hourly: list[str] = Field(min_length=1)
    max_days_per_request: int = Field(ge=1)


class CarbonConfig(_Frozen):
    max_days_per_request: int = Field(ge=1, le=14)


class EiaConfig(_Frozen):
    route: str
    type: str
    max_days_per_request: int = Field(ge=1)


class FredSeries(_Frozen):
    id: str
    unit: str


class FredConfig(_Frozen):
    series: list[FredSeries] = Field(min_length=1)


class SourcesConfig(_Frozen):
    freshness: dict[str, int]
    locations: list[Location] = Field(min_length=1)
    open_meteo: OpenMeteoConfig
    carbon_intensity: CarbonConfig
    eia: EiaConfig
    fred: FredConfig

    def max_age_hours(self, source: SourceId) -> int:
        return self.freshness[source.value]


def load_sources_config(path: Path | None = None) -> SourcesConfig:
    raw = tomllib.loads((path or default_config_path()).read_text())
    cfg = SourcesConfig.model_validate(raw)
    missing = {s.value for s in SourceId} - cfg.freshness.keys()
    if missing:
        raise ValueError(f"freshness policy missing for sources: {sorted(missing)}")
    return cfg
