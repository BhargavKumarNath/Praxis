"""Contract tests: JSON Schema (source of truth) and Pydantic model must agree."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import jsonschema
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from praxis.events import ENVELOPE_SCHEMA_VERSION, EventEnvelope, envelope_json_schema_path


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(envelope_json_schema_path().read_text())
    return loaded


def _schema_accepts(schema: dict[str, Any], doc: dict[str, Any]) -> bool:
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    return not list(validator.iter_errors(doc))


def _model_accepts(doc: dict[str, Any]) -> bool:
    try:
        EventEnvelope.model_validate(doc)
    except ValidationError:
        return False
    return True


def test_schema_is_itself_valid_and_versioned(schema: dict[str, Any]) -> None:
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["properties"]["schema_version"] == {"const": ENVELOPE_SCHEMA_VERSION}
    assert "schema_version" in schema["required"]
    assert f"v{ENVELOPE_SCHEMA_VERSION}" in envelope_json_schema_path().name


def test_trace_and_correlation_are_mandatory(schema: dict[str, Any]) -> None:
    assert {"trace_id", "correlation_id", "event_id"} <= set(schema["required"])


def test_valid_event_accepted_by_both(schema: dict[str, Any], valid_event: dict[str, Any]) -> None:
    assert _schema_accepts(schema, valid_event)
    assert _model_accepts(valid_event)


def test_round_trip_preserves_document(valid_event: dict[str, Any]) -> None:
    model = EventEnvelope.model_validate(valid_event)
    dumped = json.loads(model.model_dump_json())
    assert EventEnvelope.model_validate(dumped) == model
    assert dumped["event_id"] == valid_event["event_id"]
    assert dumped["payload"] == valid_event["payload"]


def _mutations() -> list[tuple[str, Any]]:
    def drop(key: str) -> Any:
        return lambda d: d.pop(key)

    def setk(key: str, value: Any) -> Any:
        return lambda d: d.__setitem__(key, value)

    cases: list[tuple[str, Any]] = [
        (f"missing_{k}", drop(k))
        for k in (
            "schema_version",
            "event_id",
            "event_type",
            "source",
            "occurred_at",
            "published_at",
            "trace_id",
            "correlation_id",
            "is_synthetic",
            "payload",
        )
    ]
    cases += [
        ("unsupported_version", setk("schema_version", 2)),
        ("bad_event_id", setk("event_id", "not-a-uuid")),
        ("uppercase_type", setk("event_type", "Customer.Created")),
        ("type_without_dot", setk("event_type", "customer")),
        ("bad_source", setk("source", "Sim ulator")),
        ("naive_timestamp", setk("occurred_at", "2026-10-03T12:00:00")),
        ("non_utc_timestamp", setk("occurred_at", "2026-10-03T12:00:00+02:00")),
        ("zero_trace_id", setk("trace_id", "0" * 32)),
        ("short_trace_id", setk("trace_id", "abc")),
        ("uppercase_trace_id", setk("trace_id", "4BF92F3577B34DA6A3CE929D0E0E4736")),
        ("bad_correlation", setk("correlation_id", "nope")),
        ("payload_not_object", setk("payload", [1])),
        ("synthetic_not_bool", setk("is_synthetic", "yes")),
        ("empty_entity", setk("entity_id", "")),
        ("unknown_field", setk("surprise", 1)),
    ]
    return cases


@pytest.mark.parametrize(("name", "mutate"), _mutations(), ids=[n for n, _ in _mutations()])
def test_invalid_envelope_rejected_by_both(
    name: str, mutate: Any, schema: dict[str, Any], valid_event: dict[str, Any]
) -> None:
    doc = deepcopy(valid_event)
    mutate(doc)
    assert not _schema_accepts(schema, doc), f"schema accepted {name}"
    assert not _model_accepts(doc), f"model accepted {name}"


def test_optional_fields_may_be_omitted(
    schema: dict[str, Any], valid_event: dict[str, Any]
) -> None:
    for key in ("causation_id", "entity_id"):
        doc = deepcopy(valid_event)
        del doc[key]
        assert _schema_accepts(schema, doc)
        assert _model_accepts(doc)


_hex32 = st.text(alphabet="0123456789abcdef", min_size=32, max_size=32).filter(
    lambda s: set(s) != {"0"}
)


@given(
    event_id=st.uuids(),
    trace_id=_hex32,
    correlation_id=st.uuids().map(str),
    synthetic=st.booleans(),
    ts=st.datetimes(
        timezones=st.just(UTC),
        min_value=datetime(2000, 1, 1, tzinfo=UTC),
        max_value=datetime(2100, 1, 1, tzinfo=UTC),
    ),
    payload=st.dictionaries(st.text(min_size=1, max_size=8), st.integers() | st.text(), max_size=4),
)
def test_property_round_trip(
    event_id: UUID,
    trace_id: str,
    correlation_id: str,
    synthetic: bool,
    ts: datetime,
    payload: dict[str, Any],
) -> None:
    doc = {
        "schema_version": 1,
        "event_id": str(event_id),
        "event_type": "usage.observed",
        "source": "simulator",
        "occurred_at": ts.isoformat(),
        "published_at": ts.isoformat(),
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "is_synthetic": synthetic,
        "payload": payload,
    }
    model = EventEnvelope.model_validate(doc)
    again = EventEnvelope.model_validate_json(model.model_dump_json())
    assert again == model
