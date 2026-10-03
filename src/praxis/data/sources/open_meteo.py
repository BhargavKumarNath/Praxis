"""Open-Meteo historical weather (https://open-meteo.com/en/docs/historical-weather-api).

Requests ``timezone=GMT`` so every timestamp is UTC; the response is rejected if it says
otherwise. Hourly nulls are skipped and counted.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from praxis.data.config import Location, OpenMeteoConfig
from praxis.data.errors import SchemaDriftError
from praxis.data.models import ParsedBatch, RequestSpec, SignalRecord, SourceId, TimeWindow
from praxis.data.sources.base import (
    chunk_window,
    fingerprint_json,
    load_json,
    make_record,
    summarise,
    validate_model,
)

BASE_URL = "https://archive-api.open-meteo.com/v1/archive"
_VOLATILE = frozenset({"generationtime_ms"})  # server timing, differs on every response


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")

    utc_offset_seconds: int
    timezone: str
    hourly_units: dict[str, str]
    hourly: dict[str, list[str | float | int | None]]


class OpenMeteoSource:
    source_id = SourceId.OPEN_METEO

    def __init__(
        self, config: OpenMeteoConfig, locations: list[Location], base_url: str = BASE_URL
    ) -> None:
        self._config = config
        self._locations = locations
        self._base_url = base_url

    def fingerprint(self, body: bytes) -> bytes:
        return fingerprint_json(body, _VOLATILE)

    def credential_params(self) -> dict[str, str]:
        return {}

    def build_requests(self, window: TimeWindow) -> list[RequestSpec]:
        return [
            RequestSpec(
                source=self.source_id,
                url=self._base_url,
                params={
                    "latitude": f"{loc.latitude}",
                    "longitude": f"{loc.longitude}",
                    "start_date": chunk.start.isoformat(),
                    "end_date": chunk.end.isoformat(),
                    "hourly": ",".join(self._config.hourly),
                    "timezone": "GMT",
                },
                series_id=f"open_meteo:{loc.region_id}",
                entity_id=loc.region_id,
            )
            for loc in self._locations
            for chunk in chunk_window(window, self._config.max_days_per_request)
        ]

    def parse(
        self, body: bytes, request: RequestSpec, *, batch_id: str, retrieved_at: datetime
    ) -> ParsedBatch:
        resp = validate_model(_Response, load_json(body))
        if resp.timezone != "GMT" or resp.utc_offset_seconds != 0:
            raise SchemaDriftError(f"expected GMT timestamps, got {resp.timezone}")
        times = resp.hourly.get("time")
        if times is None:
            raise SchemaDriftError("hourly.time missing")
        entity = request.entity_id or "unknown"
        records: list[SignalRecord] = []
        skipped = 0
        for metric in self._requested(request):
            values = resp.hourly.get(metric)
            if values is None or metric not in resp.hourly_units:
                raise SchemaDriftError(f"variable '{metric}' missing from response")
            if len(values) != len(times):
                raise SchemaDriftError(f"variable '{metric}' length differs from time axis")
            for ts, value in zip(times, values, strict=True):
                if value is None:
                    skipped += 1
                    continue
                if isinstance(value, str):
                    raise SchemaDriftError(f"variable '{metric}' contains a string value")
                try:
                    observed = datetime.fromisoformat(str(ts)).replace(tzinfo=UTC)
                except ValueError as exc:
                    raise SchemaDriftError("unparseable hourly timestamp") from exc
                records.append(
                    make_record(
                        request,
                        entity_id=entity,
                        metric=metric,
                        unit=resp.hourly_units[metric],
                        observed_at=observed,
                        value=float(value),
                        batch_id=batch_id,
                        retrieved_at=retrieved_at,
                    )
                )
        return summarise(records, skipped)

    @staticmethod
    def _requested(request: RequestSpec) -> list[str]:
        hourly = request.params["hourly"]
        text = hourly if isinstance(hourly, str) else ",".join(hourly)
        return text.split(",")
