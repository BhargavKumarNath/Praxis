"""Payload contracts for the 13 Phase 1 event types."""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from scripts.export_payload_schemas import OUT, render

from praxis.events.payloads import PAYLOAD_MODELS, UnknownEventType, UsageObserved, validate_payload

REQUIRED_EVENT_TYPES = {
    "customer.created",
    "usage.observed",
    "request.completed",
    "subscription.started",
    "subscription.changed",
    "price.exposed",
    "conversion.observed",
    "churn.observed",
    "invoice.created",
    "payment.attempted",
    "payment.failed",
    "payment.succeeded",
    "service.metric_observed",
}

VALID: dict[str, dict[str, Any]] = {
    "customer.created": {
        "region_id": "eu_west",
        "industry": "saas",
        "tier": "growth",
        "preferred_payment_method": "card",
        "is_existing": True,
        "tenure_days": 3,
    },
    "usage.observed": {
        "product": "api_requests",
        "region_id": "eu_west",
        "units": 5,
        "throttled_units": 0,
        "unit_price_micros": 400000,
    },
    "request.completed": {
        "region_id": "eu_west",
        "request_count": 1000,
        "error_count": 2,
        "latency_p50_ms": 40.0,
        "latency_p95_ms": 90.0,
    },
    "subscription.started": {
        "tier": "growth",
        "products": ["api_requests"],
        "origin": "new",
        "base_fee_minor": 9900,
        "billing_period_days": 30,
    },
    "subscription.changed": {
        "from_tier": "growth",
        "to_tier": "enterprise",
        "base_fee_minor": 99900,
    },
    "price.exposed": {
        "product": "api_requests",
        "unit_price_micros": 400000,
        "list_price_micros": 400000,
        "experiment_id": None,
        "arm": None,
    },
    "conversion.observed": {"converted": True, "price_index_milli": 1000},
    "churn.observed": {"reason": "voluntary", "tenure_days": 10},
    "invoice.created": {
        "invoice_id": "inv_1",
        "amount_minor": 1000,
        "currency": "GBP",
        "period_start": "2026-01-01",
        "period_end": "2026-01-30",
        "tier": "growth",
    },
    "payment.attempted": {
        "invoice_id": "inv_1",
        "attempt_number": 1,
        "amount_minor": 1000,
        "currency": "GBP",
        "payment_method": "card",
    },
    "payment.failed": {
        "invoice_id": "inv_1",
        "attempt_number": 1,
        "amount_minor": 1000,
        "currency": "GBP",
        "reason": "card_declined",
        "final": False,
    },
    "payment.succeeded": {
        "invoice_id": "inv_1",
        "attempt_number": 1,
        "amount_minor": 1000,
        "currency": "GBP",
    },
    "service.metric_observed": {
        "region_id": "eu_west",
        "capacity_units": 100,
        "utilization": 0.5,
        "latency_p50_ms": 40.0,
        "latency_p95_ms": 90.0,
        "error_rate": 0.001,
        "available_products": ["api_requests"],
        "marginal_cost_micros": {"api_requests": 120000},
    },
}


def test_exactly_the_required_event_types_are_contracted() -> None:
    assert set(PAYLOAD_MODELS) == REQUIRED_EVENT_TYPES == set(VALID)


@pytest.mark.parametrize("etype", sorted(VALID))
def test_valid_payload_round_trips(etype: str) -> None:
    model = validate_payload(etype, VALID[etype])
    assert json.loads(model.model_dump_json()) == VALID[etype]


@pytest.mark.parametrize("etype", sorted(VALID))
def test_unknown_field_is_rejected(etype: str) -> None:
    with pytest.raises(ValidationError):
        validate_payload(etype, {**VALID[etype], "surprise": 1})


@pytest.mark.parametrize("etype", sorted(VALID))
def test_missing_required_field_is_rejected(etype: str) -> None:
    required = {k for k, f in PAYLOAD_MODELS[etype].model_fields.items() if f.is_required()}
    assert required
    for key in required:
        bad = {k: v for k, v in VALID[etype].items() if k != key}
        with pytest.raises(ValidationError):
            validate_payload(etype, bad)


@pytest.mark.parametrize(
    ("etype", "patch"),
    [
        ("usage.observed", {"units": 0}),
        ("usage.observed", {"units": 1.5}),
        ("usage.observed", {"throttled_units": -1}),
        ("usage.observed", {"unit_price_micros": 0}),
        ("customer.created", {"tier": "platinum"}),
        ("customer.created", {"tenure_days": -1}),
        ("invoice.created", {"currency": "USD"}),
        ("invoice.created", {"amount_minor": 10.5}),
        ("invoice.created", {"period_start": "01/01/2026"}),
        ("payment.failed", {"reason": "bad_vibes"}),
        ("payment.failed", {"final": "yes"}),
        ("payment.attempted", {"attempt_number": 0}),
        ("subscription.started", {"products": []}),
        ("churn.observed", {"reason": "bored"}),
        ("service.metric_observed", {"error_rate": 1.5}),
        ("service.metric_observed", {"utilization": -0.1}),
        ("price.exposed", {"arm": "maybe"}),
    ],
)
def test_invalid_values_are_rejected(etype: str, patch: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        validate_payload(etype, {**VALID[etype], **patch})


def test_unknown_event_type_is_rejected() -> None:
    with pytest.raises(UnknownEventType):
        validate_payload("customer.exploded", {})


def test_exported_json_schemas_are_current_and_versioned() -> None:
    rendered = render()
    assert len(rendered) == len(REQUIRED_EVENT_TYPES)
    for name, text in rendered.items():
        assert (OUT / name).read_text() == text, (
            f"{name} is stale: run scripts/export_payload_schemas.py"
        )
        schema = json.loads(text)
        assert ".v1." in name and schema["additionalProperties"] is False


def test_money_fields_are_integers_not_floats() -> None:
    for etype, model in PAYLOAD_MODELS.items():
        for fname, field in model.model_fields.items():
            if fname.endswith(("_minor", "_micros")):
                assert "float" not in str(field.annotation), f"{etype}.{fname}"


@given(units=st.integers(1, 10**9), thr=st.integers(0, 10**9), price=st.integers(1, 10**9))
def test_property_usage_round_trip(units: int, thr: int, price: int) -> None:
    payload = {
        **VALID["usage.observed"],
        "units": units,
        "throttled_units": thr,
        "unit_price_micros": price,
    }
    model = UsageObserved.model_validate(payload)
    assert UsageObserved.model_validate_json(model.model_dump_json()) == model
