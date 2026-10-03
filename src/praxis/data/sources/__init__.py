"""Source registry: builds every configured adapter from config and settings."""

from __future__ import annotations

from pydantic import SecretStr

from praxis.data.config import SourcesConfig
from praxis.data.models import SourceId
from praxis.data.sources.base import Source
from praxis.data.sources.carbon_intensity import CarbonIntensitySource
from praxis.data.sources.eia import EiaSource
from praxis.data.sources.fred import FredSource
from praxis.data.sources.open_meteo import OpenMeteoSource


def build_registry(
    config: SourcesConfig,
    *,
    fred_api_key: SecretStr | None = None,
    eia_api_key: SecretStr | None = None,
) -> dict[SourceId, Source]:
    sources: list[Source] = [
        OpenMeteoSource(config.open_meteo, config.locations),
        CarbonIntensitySource(config.carbon_intensity),
        EiaSource(config.eia, config.locations, eia_api_key),
        FredSource(config.fred, fred_api_key),
    ]
    return {s.source_id: s for s in sources}


__all__ = ["Source", "build_registry"]
