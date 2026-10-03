"""Shared helpers for data-platform tests: fixtures, mock transports, fixed clock."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from praxis.data.config import SourcesConfig, load_sources_config
from praxis.data.fetch import HttpFetcher, RetryPolicy
from praxis.data.models import SourceId, TimeWindow
from praxis.data.sources import build_registry

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
WINDOW = TimeWindow(start=date(2026, 1, 5), end=date(2026, 1, 5))
FAKE_FRED_KEY = "f" * 32  # not a real key; built to look like one
FAKE_EIA_KEY = "e" * 40

FIXTURE_FOR_SOURCE = {
    SourceId.OPEN_METEO: "open_meteo_london_2026-01-05.json",
    SourceId.CARBON_INTENSITY: "carbon_gb_2026-01-05.json",
    SourceId.FRED: "fred_cpiaucsl.json",
    SourceId.EIA: "eia_ny_demand.json",
}


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def cfg() -> SourcesConfig:
    return load_sources_config()


def registry(**keys: object) -> dict:  # type: ignore[type-arg]
    from pydantic import SecretStr

    return build_registry(
        cfg(),
        fred_api_key=SecretStr(FAKE_FRED_KEY) if keys.get("fred", True) else None,
        eia_api_key=SecretStr(FAKE_EIA_KEY) if keys.get("eia", True) else None,
    )


def fixture_handler(
    overrides: dict[SourceId, bytes] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve fixture bytes keyed by which API host the request targets."""
    overrides = overrides or {}

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if "open-meteo" in host:
            sid = SourceId.OPEN_METEO
        elif "carbonintensity" in host:
            sid = SourceId.CARBON_INTENSITY
        elif "stlouisfed" in host:
            sid = SourceId.FRED
        else:
            sid = SourceId.EIA
        body = overrides.get(sid, fixture_bytes(FIXTURE_FOR_SOURCE[sid]))
        if sid is SourceId.EIA and sid not in overrides:
            respondent = request.url.params.get("facets[respondent][]", "NY")
            body = body.replace(b'"NY"', f'"{respondent}"'.encode())
        return httpx.Response(200, content=body)

    return handler


def make_fetcher(
    handler: Callable[[httpx.Request], httpx.Response], sleeps: list[float] | None = None
) -> HttpFetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    record = sleeps if sleeps is not None else []
    return HttpFetcher(client, RetryPolicy(), sleeper=record.append)
