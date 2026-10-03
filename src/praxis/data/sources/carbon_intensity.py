"""NESO Carbon Intensity API, national GB half-hourly (https://carbon-intensity.github.io/api-definitions/).

The window is encoded in the URL path. ``actual`` is null for slots not yet measured; those
are skipped and counted rather than stored as zero.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from praxis.data.config import CarbonConfig
from praxis.data.errors import SchemaDriftError
from praxis.data.models import ParsedBatch, RequestSpec, SignalRecord, SourceId, TimeWindow
from praxis.data.sources.base import (
    chunk_window,
    day_start,
    fingerprint_json,
    load_json,
    make_record,
    summarise,
    validate_model,
)

BASE_URL = "https://api.carbonintensity.org.uk"
_TS_FORMAT = "%Y-%m-%dT%H:%MZ"
UNIT = "gCO2/kWh"


class _Intensity(BaseModel):
    model_config = ConfigDict(extra="ignore")
    forecast: int | None
    actual: int | None
    index: str | None = None


class _Slot(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    from_: str = Field(alias="from")
    intensity: _Intensity


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")
    data: list[dict[str, object]]


class CarbonIntensitySource:
    source_id = SourceId.CARBON_INTENSITY

    def __init__(self, config: CarbonConfig, base_url: str = BASE_URL) -> None:
        self._config = config
        self._base_url = base_url

    def fingerprint(self, body: bytes) -> bytes:
        return fingerprint_json(body, frozenset())

    def credential_params(self) -> dict[str, str]:
        return {}

    def build_requests(self, window: TimeWindow) -> list[RequestSpec]:
        out: list[RequestSpec] = []
        for chunk in chunk_window(window, self._config.max_days_per_request):
            start = day_start(chunk.start)
            end = day_start(chunk.end) + timedelta(days=1)
            out.append(
                RequestSpec(
                    source=self.source_id,
                    url=f"{self._base_url}/intensity/"
                    f"{start.strftime(_TS_FORMAT)}/{end.strftime(_TS_FORMAT)}",
                    params={},
                    series_id="carbon_intensity:gb",
                    entity_id="gb",
                )
            )
        return out

    def parse(
        self, body: bytes, request: RequestSpec, *, batch_id: str, retrieved_at: datetime
    ) -> ParsedBatch:
        resp = validate_model(_Response, load_json(body))
        records: list[SignalRecord] = []
        skipped = 0
        for raw in resp.data:
            slot = validate_model(_Slot, raw)
            try:
                observed = datetime.strptime(slot.from_, _TS_FORMAT).replace(tzinfo=UTC)
            except ValueError as exc:
                raise SchemaDriftError("unparseable slot timestamp") from exc
            for metric, value in (
                ("carbon_intensity_actual", slot.intensity.actual),
                ("carbon_intensity_forecast", slot.intensity.forecast),
            ):
                if value is None:
                    skipped += 1
                    continue
                records.append(
                    make_record(
                        request,
                        entity_id="gb",
                        metric=metric,
                        unit=UNIT,
                        observed_at=observed,
                        value=float(value),
                        batch_id=batch_id,
                        retrieved_at=retrieved_at,
                    )
                )
        return summarise(records, skipped)
