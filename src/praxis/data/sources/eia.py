"""EIA API v2 hourly regional demand (https://www.eia.gov/opendata/documentation.php).

Windows are chunked so one request stays well under the 5,000 row cap. If the API reports
more rows than it returned the batch is treated as truncated and quarantined, never silently
accepted as complete.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from praxis.data.config import EiaConfig, Location
from praxis.data.errors import SchemaDriftError
from praxis.data.models import ParsedBatch, RequestSpec, SignalRecord, SourceId, TimeWindow
from praxis.data.sources.base import (
    chunk_window,
    fingerprint_json,
    load_json,
    make_record,
    require_secret,
    summarise,
    validate_model,
)

BASE_URL = "https://api.eia.gov/v2"
ROW_CAP = 5000
METRIC = "electricity_demand"


class _Row(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    period: str
    respondent: str
    type: str
    value: str | int | float | None
    units: str = Field(alias="value-units")


class _Inner(BaseModel):
    model_config = ConfigDict(extra="ignore")
    total: int
    data: list[_Row]


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")
    response: _Inner


class EiaSource:
    source_id = SourceId.EIA

    def __init__(
        self,
        config: EiaConfig,
        locations: list[Location],
        api_key: SecretStr | None,
        base_url: str = BASE_URL,
    ) -> None:
        self._config = config
        self._respondents = [loc.eia_respondent for loc in locations if loc.eia_respondent]
        self._api_key = api_key
        self._base_url = base_url

    def fingerprint(self, body: bytes) -> bytes:
        return fingerprint_json(body, frozenset())

    def credential_params(self) -> dict[str, str]:
        return require_secret("PRAXIS_EIA_API_KEY", self._api_key)

    def build_requests(self, window: TimeWindow) -> list[RequestSpec]:
        return [
            RequestSpec(
                source=self.source_id,
                url=f"{self._base_url}/{self._config.route}",
                params={
                    "frequency": "hourly",
                    "data[0]": "value",
                    "facets[respondent][]": [respondent],
                    "facets[type][]": [self._config.type],
                    "start": f"{chunk.start.isoformat()}T00",
                    "end": f"{chunk.end.isoformat()}T23",
                    "sort[0][column]": "period",
                    "sort[0][direction]": "asc",
                    "offset": "0",
                    "length": str(ROW_CAP),
                },
                series_id=f"eia:{respondent}:{self._config.type}",
                entity_id=respondent,
            )
            for respondent in self._respondents
            for chunk in chunk_window(window, self._config.max_days_per_request)
        ]

    def parse(
        self, body: bytes, request: RequestSpec, *, batch_id: str, retrieved_at: datetime
    ) -> ParsedBatch:
        inner = validate_model(_Response, load_json(body)).response
        if inner.total > len(inner.data):
            raise SchemaDriftError(
                f"truncated response: total={inner.total}, returned={len(inner.data)}"
            )
        expected = request.entity_id
        records: list[SignalRecord] = []
        skipped = 0
        for row in inner.data:
            if row.respondent != expected or row.type != self._config.type:
                raise SchemaDriftError("row outside the requested respondent/type facet")
            if row.value is None:
                skipped += 1
                continue
            try:
                value = float(row.value)
                observed = datetime.strptime(row.period, "%Y-%m-%dT%H").replace(tzinfo=UTC)
            except ValueError as exc:
                raise SchemaDriftError("unparseable EIA row") from exc
            records.append(
                make_record(
                    request,
                    entity_id=row.respondent,
                    metric=METRIC,
                    unit=row.units,
                    observed_at=observed,
                    value=value,
                    batch_id=batch_id,
                    retrieved_at=retrieved_at,
                )
            )
        return summarise(records, skipped)
