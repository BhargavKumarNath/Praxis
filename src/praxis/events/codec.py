"""Wire format for events on the bus, plus decode-time error classification.

Body: the canonical JSON envelope (sorted keys, compact). Attributes duplicate a few
envelope fields so that routing (subscription filters), tracing and dead-letter triage
work even when the body cannot be parsed.

Every decode failure is a ``DecodeError`` with a stable ``reason``. Decode failures are
*permanent*: redelivering the same bytes can never succeed, so consumers dead-letter them
immediately instead of burning retries.

Schema versions: ``SUPPORTED_ENVELOPE_VERSIONS`` lists the envelope versions this build can
read. Anything else is rejected with ``unsupported_schema_version`` (checked before full
validation so the reason is specific). A future v2 adds an upcaster here, never a silent
best-effort parse.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from praxis.domain.projections import STATEFUL_EVENT_TYPES
from praxis.events.envelope import EventEnvelope
from praxis.events.payloads import PAYLOAD_MODELS, validate_payload

SUPPORTED_ENVELOPE_VERSIONS = frozenset({1})
MAX_MESSAGE_BYTES = 64 * 1024  # simulator events are < 2 KiB; Pub/Sub allows 10 MB

# Attribute names (Pub/Sub attribute keys must not start with "goog").
ATTR_EVENT_ID = "event_id"
ATTR_EVENT_TYPE = "event_type"
ATTR_SCHEMA_VERSION = "schema_version"
ATTR_SOURCE = "source"
ATTR_TRACE_ID = "trace_id"
ATTR_CORRELATION_ID = "correlation_id"
ATTR_STATEFUL = "stateful"  # "true" when the event mutates control-plane state
ATTR_REPLAY = "praxis_replay"
ATTR_SENT_AT = "praxis_sent_at"  # producer wall-clock send time, epoch seconds


class DecodeError(ValueError):
    REASONS = frozenset(
        {
            "oversized",
            "malformed_json",
            "invalid_envelope",
            "unsupported_schema_version",
            "unknown_event_type",
            "invalid_payload",
            "attribute_mismatch",
        }
    )

    def __init__(self, reason: str, detail: str) -> None:
        if reason not in self.REASONS:
            raise ValueError(f"unknown decode reason {reason!r}")
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class DecodedEvent:
    envelope: EventEnvelope
    payload: BaseModel

    @property
    def event_id(self) -> str:
        return str(self.envelope.event_id)

    @property
    def event_type(self) -> str:
        return self.envelope.event_type

    @property
    def trace_id(self) -> str:
        return self.envelope.trace_id

    @property
    def correlation_id(self) -> str:
        return self.envelope.correlation_id

    @property
    def raw_payload(self) -> dict[str, Any]:
        return self.envelope.payload


def canonical_bytes(event: Mapping[str, Any]) -> bytes:
    return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str).encode()


def attributes_for(event: Mapping[str, Any]) -> dict[str, str]:
    return {
        ATTR_EVENT_ID: str(event["event_id"]),
        ATTR_EVENT_TYPE: str(event["event_type"]),
        ATTR_SCHEMA_VERSION: str(event["schema_version"]),
        ATTR_SOURCE: str(event["source"]),
        ATTR_TRACE_ID: str(event["trace_id"]),
        ATTR_CORRELATION_ID: str(event["correlation_id"]),
        ATTR_STATEFUL: "true" if event["event_type"] in STATEFUL_EVENT_TYPES else "false",
    }


def encode(event: Mapping[str, Any]) -> tuple[bytes, dict[str, str]]:
    return canonical_bytes(event), attributes_for(event)


def validate_event(event: Mapping[str, Any]) -> DecodedEvent:
    """Validate an already-parsed event dict. Raises ``DecodeError``."""
    version = event.get("schema_version")
    if type(version) is not int or version not in SUPPORTED_ENVELOPE_VERSIONS:
        raise DecodeError("unsupported_schema_version", f"schema_version={version!r}")
    try:
        envelope = EventEnvelope.model_validate(event)
    except ValidationError as exc:
        raise DecodeError("invalid_envelope", _summarise(exc)) from None
    if envelope.event_type not in PAYLOAD_MODELS:
        raise DecodeError("unknown_event_type", envelope.event_type)
    try:
        payload = validate_payload(envelope.event_type, envelope.payload)
    except ValidationError as exc:
        raise DecodeError("invalid_payload", _summarise(exc)) from None
    return DecodedEvent(envelope, payload)


def decode(data: bytes, attributes: Mapping[str, str] | None = None) -> DecodedEvent:
    if len(data) > MAX_MESSAGE_BYTES:
        raise DecodeError("oversized", f"{len(data)} bytes")
    try:
        parsed = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DecodeError("malformed_json", type(exc).__name__) from None
    if not isinstance(parsed, dict):
        raise DecodeError("invalid_envelope", f"top-level {type(parsed).__name__}")
    decoded = validate_event(parsed)
    if attributes:
        for key, actual in (
            (ATTR_EVENT_ID, decoded.event_id),
            (ATTR_EVENT_TYPE, decoded.event_type),
        ):
            claimed = attributes.get(key)
            if claimed is not None and claimed != actual:
                raise DecodeError("attribute_mismatch", f"{key} attribute differs from body")
    return decoded


def _summarise(exc: ValidationError) -> str:
    """Field locations and error types only: never echo input values (could be sensitive)."""
    parts = [f"{'.'.join(str(p) for p in err['loc'])}:{err['type']}" for err in exc.errors()]
    return ", ".join(parts[:5])[:300]
