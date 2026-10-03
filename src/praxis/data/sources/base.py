"""Source adapter contract and shared helpers.

An adapter is pure: it builds requests and normalises bytes. It never performs I/O, so the
same ``parse`` serves live ingestion and raw replay.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, SecretStr, ValidationError

from praxis.data.errors import MissingCredentialError, SchemaDriftError
from praxis.data.models import (
    ParsedBatch,
    RequestSpec,
    SignalRecord,
    SourceId,
    TimeWindow,
    make_record_key,
)


class Source(Protocol):
    source_id: SourceId

    def build_requests(self, window: TimeWindow) -> list[RequestSpec]: ...

    def credential_params(self) -> dict[str, str]:
        """Credentials to add at fetch time. Never stored, never logged."""
        ...

    def fingerprint(self, body: bytes) -> bytes:
        """Content identity of a body: the bytes minus fields that change on every response."""
        ...

    def parse(
        self, body: bytes, request: RequestSpec, *, batch_id: str, retrieved_at: datetime
    ) -> ParsedBatch: ...


def chunk_window(window: TimeWindow, max_days: int) -> Iterator[TimeWindow]:
    cursor = window.start
    while cursor <= window.end:
        last = min(cursor + timedelta(days=max_days - 1), window.end)
        yield TimeWindow(start=cursor, end=last)
        cursor = last + timedelta(days=1)


def day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def fingerprint_json(body: bytes, volatile: frozenset[str] = frozenset()) -> bytes:
    """Canonical JSON without volatile top-level keys; falls back to the raw bytes."""
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if k not in volatile}
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def load_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaDriftError(f"body is not valid JSON: {type(exc).__name__}") from exc


def validate_model[M: BaseModel](model: type[M], data: Any) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise SchemaDriftError(f"contract violation at '{loc}': {first['type']}") from exc


def require_secret(name: str, secret: SecretStr | None) -> dict[str, str]:
    if secret is None or not secret.get_secret_value():
        raise MissingCredentialError(f"{name} is not configured")
    return {"api_key": secret.get_secret_value()}


def make_record(
    request: RequestSpec,
    *,
    entity_id: str,
    metric: str,
    unit: str,
    observed_at: datetime,
    value: float,
    batch_id: str,
    retrieved_at: datetime,
) -> SignalRecord:
    return SignalRecord(
        record_key=make_record_key(
            request.source, request.series_id, entity_id, metric, observed_at
        ),
        source=request.source,
        series_id=request.series_id,
        entity_id=entity_id,
        metric=metric,
        unit=unit,
        observed_at=observed_at,
        value=value,
        batch_id=batch_id,
        retrieved_at=retrieved_at,
    )


def summarise(records: list[SignalRecord], skipped: int) -> ParsedBatch:
    times = [r.observed_at for r in records]
    return ParsedBatch(
        records=records,
        skipped=skipped,
        source_timestamp_min=min(times) if times else None,
        source_timestamp_max=max(times) if times else None,
    )
