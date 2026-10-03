"""Versioned event envelope (contract v1).

The JSON Schema at ``schemas/events/event_envelope.v1.schema.json`` is the
language-neutral source of truth. This Pydantic model mirrors it; contract tests assert
both accept and reject the same documents.

Delivery is at-least-once, so ``event_id`` is the idempotency key consumers dedupe on.
``payload`` is opaque here; per-event-type payload schemas arrive with their producers.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool

from praxis.tracing import TRACE_ID_PATTERN, is_valid_correlation_id

ENVELOPE_SCHEMA_VERSION = 1
EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
SOURCE_PATTERN = r"^[a-z][a-z0-9_-]*$"


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    if value.utcoffset().total_seconds() != 0:  # type: ignore[union-attr]
        raise ValueError("timestamp must be UTC")
    return value


def _check_correlation(value: str) -> str:
    if not is_valid_correlation_id(value):
        raise ValueError("correlation_id must be a canonical lowercase UUID string")
    return value


UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]
CorrelationId = Annotated[str, AfterValidator(_check_correlation)]


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, regex_engine="python-re")

    schema_version: Literal[1]
    event_id: UUID
    event_type: str = Field(pattern=EVENT_TYPE_PATTERN, max_length=128)
    source: str = Field(pattern=SOURCE_PATTERN, max_length=64)
    occurred_at: UtcDatetime
    published_at: UtcDatetime
    trace_id: str = Field(pattern=TRACE_ID_PATTERN)
    correlation_id: CorrelationId
    causation_id: UUID | None = None
    entity_id: str | None = Field(default=None, min_length=1, max_length=128)
    is_synthetic: StrictBool
    payload: dict[str, Any]


def envelope_json_schema_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "schemas"
        / "events"
        / (f"event_envelope.v{ENVELOPE_SCHEMA_VERSION}.schema.json")
    )
