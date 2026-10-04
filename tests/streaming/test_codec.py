"""Wire contract: round trip, attributes, and every permanent decode failure."""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from praxis.events.codec import (
    ATTR_EVENT_ID,
    ATTR_STATEFUL,
    MAX_MESSAGE_BYTES,
    DecodeError,
    canonical_bytes,
    decode,
    encode,
)
from tests.streaming.helpers import customer_lifecycle, invoice_lifecycle, sim_events

SAMPLE = [*customer_lifecycle(), *invoice_lifecycle()]


@settings(max_examples=200, deadline=None)
@given(st.sampled_from(list(sim_events(50, 14))))
def test_round_trip_preserves_every_field(event: dict[str, Any]) -> None:
    data, attrs = encode(event)
    decoded = decode(data, attrs)
    assert decoded.envelope.model_dump(mode="json") == json.loads(canonical_bytes(event))
    assert attrs[ATTR_EVENT_ID] == decoded.event_id == event["event_id"]
    assert decoded.trace_id == event["trace_id"]
    assert decoded.correlation_id == event["correlation_id"]


def test_stateful_attribute_routes_only_lifecycle_events() -> None:
    flags = {e["event_type"]: encode(e)[1][ATTR_STATEFUL] for e in sim_events(50, 14)}
    assert flags["usage.observed"] == flags["service.metric_observed"] == "false"
    assert flags["customer.created"] == flags["invoice.created"] == "true"


def _mutate(**changes: Any) -> bytes:
    event = dict(SAMPLE[0])
    event.update(changes)
    return json.dumps(event).encode()


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"\xff\xfe not json", "malformed_json"),
        (b"{not json", "malformed_json"),
        (b"[1, 2]", "invalid_envelope"),
        (_mutate(schema_version=2), "unsupported_schema_version"),
        (_mutate(schema_version="1"), "unsupported_schema_version"),
        (_mutate(trace_id="not-a-trace"), "invalid_envelope"),
        (_mutate(occurred_at="2026-01-05T00:00:00"), "invalid_envelope"),
        (_mutate(event_type="refund.issued"), "unknown_event_type"),
        (_mutate(payload={"tier": "platinum"}), "invalid_payload"),
        (b"{" + b" " * MAX_MESSAGE_BYTES + b"}", "oversized"),
    ],
)
def test_decode_failures_have_stable_reasons(data: bytes, reason: str) -> None:
    with pytest.raises(DecodeError) as info:
        decode(data)
    assert info.value.reason == reason


def test_attribute_body_mismatch_is_rejected() -> None:
    data, attrs = encode(SAMPLE[0])
    attrs[ATTR_EVENT_ID] = SAMPLE[1]["event_id"]
    with pytest.raises(DecodeError, match="attribute_mismatch"):
        decode(data, attrs)


def test_decode_errors_never_echo_values() -> None:
    secretish = "4111111111111111"
    with pytest.raises(DecodeError) as info:
        decode(_mutate(payload={"region_id": secretish}))
    assert secretish not in str(info.value)


def test_unknown_reason_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="unknown decode reason"):
        DecodeError("nope", "x")
