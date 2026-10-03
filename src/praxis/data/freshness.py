"""Stale-source detection against per-source policies in ``configs/data/sources.toml``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from praxis.data.config import SourcesConfig
from praxis.data.models import SourceId
from praxis.data.warehouse import Warehouse


class FreshnessState(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"  # nothing ingested yet


@dataclass(frozen=True)
class FreshnessResult:
    source: SourceId
    state: FreshnessState
    latest_observation: datetime | None
    max_age: timedelta


def check_freshness(
    warehouse: Warehouse, config: SourcesConfig, now: datetime
) -> list[FreshnessResult]:
    """Newest observation (not retrieval time) must be within the policy; age == max is fresh."""
    results: list[FreshnessResult] = []
    for source in SourceId:
        max_age = timedelta(hours=config.max_age_hours(source))
        latest = warehouse.latest_observation(source, now)
        if latest is None:
            state = FreshnessState.MISSING
        elif now - latest > max_age:
            state = FreshnessState.STALE
        else:
            state = FreshnessState.FRESH
        results.append(FreshnessResult(source, state, latest, max_age))
    return results
