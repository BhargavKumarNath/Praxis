"""FRED macro series (https://fred.stlouisfed.org/docs/api/fred/series_observations.html).

Missing observations arrive as the string ``"."``; they are skipped and counted. The window
filters on observation date. A revised value re-ingested later overwrites by record key; the
raw archive keeps every vintage.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, SecretStr

from praxis.data.config import FredConfig
from praxis.data.errors import SchemaDriftError
from praxis.data.models import ParsedBatch, RequestSpec, SignalRecord, SourceId, TimeWindow
from praxis.data.sources.base import (
    day_start,
    fingerprint_json,
    load_json,
    make_record,
    require_secret,
    summarise,
    validate_model,
)

BASE_URL = "https://api.stlouisfed.org/fred/series/observations"


class _Observation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    date: str
    value: str


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")
    observations: list[_Observation]


class FredSource:
    source_id = SourceId.FRED

    def __init__(
        self, config: FredConfig, api_key: SecretStr | None, base_url: str = BASE_URL
    ) -> None:
        self._config = config
        self._api_key = api_key
        self._base_url = base_url

    def fingerprint(self, body: bytes) -> bytes:
        return fingerprint_json(body, frozenset())

    def credential_params(self) -> dict[str, str]:
        return require_secret("PRAXIS_FRED_API_KEY", self._api_key)

    def build_requests(self, window: TimeWindow) -> list[RequestSpec]:
        return [
            RequestSpec(
                source=self.source_id,
                url=self._base_url,
                params={
                    "series_id": s.id,
                    "file_type": "json",
                    "observation_start": window.start.isoformat(),
                    "observation_end": window.end.isoformat(),
                },
                series_id=f"fred:{s.id}",
                entity_id="us",
            )
            for s in self._config.series
        ]

    def parse(
        self, body: bytes, request: RequestSpec, *, batch_id: str, retrieved_at: datetime
    ) -> ParsedBatch:
        resp = validate_model(_Response, load_json(body))
        series = request.params["series_id"]
        unit = next((s.unit for s in self._config.series if s.id == series), None)
        if unit is None:
            raise SchemaDriftError(f"series '{series}' is not configured")
        records: list[SignalRecord] = []
        skipped = 0
        for obs in resp.observations:
            if obs.value == ".":
                skipped += 1
                continue
            try:
                value = float(obs.value)
                observed = day_start(date.fromisoformat(obs.date))
            except ValueError as exc:
                raise SchemaDriftError("unparseable FRED observation") from exc
            records.append(
                make_record(
                    request,
                    entity_id="us",
                    metric=str(series).lower(),
                    unit=unit,
                    observed_at=observed,
                    value=value,
                    batch_id=batch_id,
                    retrieved_at=retrieved_at,
                )
            )
        return summarise(records, skipped)
