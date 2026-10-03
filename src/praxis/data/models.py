"""Typed records shared by every source, the raw store and the warehouse."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

# Bumped when the normalised record or provenance shape changes.
SIGNAL_SCHEMA_VERSION = 1


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]


class SourceId(StrEnum):
    OPEN_METEO = "open_meteo"
    CARBON_INTENSITY = "carbon_intensity"
    EIA = "eia"
    FRED = "fred"


class QualityStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"  # parsed, but some rows were unusable (nulls, missing markers)
    QUARANTINED = "quarantined"  # contract violated; no normalised rows were produced


class TimeWindow(BaseModel):
    """Inclusive UTC calendar-day window of observations to request."""

    model_config = ConfigDict(frozen=True)

    start: date
    end: date

    def model_post_init(self, __context: Any) -> None:
        if self.end < self.start:
            raise ValueError("window end precedes start")


class RequestSpec(BaseModel):
    """One concrete HTTP request. ``params`` never contains a credential."""

    model_config = ConfigDict(frozen=True)

    source: SourceId
    url: str
    params: dict[str, str | list[str]]
    series_id: str  # stable label for the endpoint series, e.g. "open_meteo:eu_west"
    entity_id: str | None = None  # region / respondent the request targets


class SignalRecord(BaseModel):
    """One normalised observation (long format). Physical signals, not money, so float."""

    model_config = ConfigDict(frozen=True)

    record_key: str
    source: SourceId
    series_id: str
    entity_id: str
    metric: str
    unit: str
    observed_at: UtcDatetime
    value: float
    batch_id: str
    retrieved_at: UtcDatetime
    schema_version: int = SIGNAL_SCHEMA_VERSION


class ParsedBatch(BaseModel):
    """Result of normalising one raw body."""

    model_config = ConfigDict(frozen=True)

    records: list[SignalRecord]
    skipped: int = Field(ge=0)
    source_timestamp_min: UtcDatetime | None
    source_timestamp_max: UtcDatetime | None

    @property
    def quality(self) -> QualityStatus:
        return QualityStatus.PARTIAL if self.skipped else QualityStatus.OK


class BatchProvenance(BaseModel):
    """Batch-level provenance, one row per fetched body (CLAUDE.md section 8)."""

    model_config = ConfigDict(frozen=True)

    batch_id: str
    source: SourceId
    series_id: str
    endpoint: str  # credential-free URL including query string
    retrieved_at: UtcDatetime
    source_timestamp_min: UtcDatetime | None = None
    source_timestamp_max: UtcDatetime | None = None
    schema_version: int = SIGNAL_SCHEMA_VERSION
    checksum_sha256: str
    http_status: int
    quality_status: QualityStatus
    quality_detail: str | None = None
    record_count: int = 0
    skipped_count: int = 0
    is_synthetic: bool = False


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_batch_id(request: RequestSpec, fingerprint: bytes) -> str:
    """Content-derived: the same request returning the same content is the same batch.

    ``fingerprint`` is the body with volatile fields (e.g. server timings) removed; see
    ``Source.fingerprint``. The raw body itself is always archived unmodified.
    """
    h = hashlib.sha256()
    h.update(request.source.value.encode())
    h.update(b"\0")
    h.update(request.model_dump_json().encode())
    h.update(b"\0")
    h.update(fingerprint)
    return h.hexdigest()


def make_record_key(
    source: SourceId, series_id: str, entity_id: str, metric: str, observed_at: datetime
) -> str:
    """Logical identity of an observation. The value is excluded so revisions overwrite."""
    text = "|".join([source.value, series_id, entity_id, metric, observed_at.isoformat()])
    return hashlib.blake2b(text.encode(), digest_size=16).hexdigest()
