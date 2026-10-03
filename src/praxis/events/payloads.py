"""Per-event-type payload contracts (v1) for the 13 Phase 1 event types.

The envelope (``envelope.py``) is generic; these models define what ``payload`` must hold
for each ``event_type``. Payloads carry observable facts only: latent simulator
parameters (ground truth) never appear in events.

Money is integer minor units (``amount_minor``, pence) or integer micro-GBP
(``*_micros``, 1e-6 GBP) for unit prices. No binary floats represent money.
JSON Schemas are exported to ``schemas/events/payloads/`` and kept in sync by a test.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

PAYLOAD_SCHEMA_VERSION = 1

Tier = Literal["starter", "growth", "enterprise"]
Currency = Literal["GBP"]
PaymentMethod = Literal["card", "direct_debit", "wallet"]
FailureReason = Literal[
    "insufficient_funds",
    "card_declined",
    "expired_card",
    "processor_error",
    "authentication_required",
]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CustomerCreated(_Payload):
    region_id: str
    industry: str
    tier: Tier
    preferred_payment_method: PaymentMethod
    is_existing: StrictBool
    tenure_days: StrictInt = Field(ge=0)


class UsageObserved(_Payload):
    product: str
    region_id: str
    units: StrictInt = Field(ge=1)
    throttled_units: StrictInt = Field(ge=0)
    unit_price_micros: StrictInt = Field(gt=0)


class RequestCompleted(_Payload):
    region_id: str
    request_count: StrictInt = Field(ge=1)
    error_count: StrictInt = Field(ge=0)
    latency_p50_ms: float = Field(gt=0)
    latency_p95_ms: float = Field(gt=0)


class SubscriptionStarted(_Payload):
    tier: Tier
    products: list[str] = Field(min_length=1)
    origin: Literal["new", "existing"]
    base_fee_minor: StrictInt = Field(ge=0)
    billing_period_days: StrictInt = Field(gt=0)


class SubscriptionChanged(_Payload):
    from_tier: Tier
    to_tier: Tier
    base_fee_minor: StrictInt = Field(ge=0)


class PriceExposed(_Payload):
    product: str
    unit_price_micros: StrictInt = Field(gt=0)
    list_price_micros: StrictInt = Field(gt=0)
    experiment_id: str | None = None
    arm: Literal["control", "treatment"] | None = None


class ConversionObserved(_Payload):
    converted: StrictBool
    price_index_milli: StrictInt = Field(gt=0)


class ChurnObserved(_Payload):
    reason: Literal["voluntary", "involuntary_payment"]
    tenure_days: StrictInt = Field(ge=0)


class InvoiceCreated(_Payload):
    invoice_id: str
    amount_minor: StrictInt = Field(gt=0)
    currency: Currency
    period_start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    period_end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    tier: Tier


class PaymentAttempted(_Payload):
    invoice_id: str
    attempt_number: StrictInt = Field(ge=1)
    amount_minor: StrictInt = Field(gt=0)
    currency: Currency
    payment_method: PaymentMethod


class PaymentFailed(_Payload):
    invoice_id: str
    attempt_number: StrictInt = Field(ge=1)
    amount_minor: StrictInt = Field(gt=0)
    currency: Currency
    reason: FailureReason
    final: StrictBool


class PaymentSucceeded(_Payload):
    invoice_id: str
    attempt_number: StrictInt = Field(ge=1)
    amount_minor: StrictInt = Field(gt=0)
    currency: Currency


class ServiceMetricObserved(_Payload):
    region_id: str
    capacity_units: StrictInt = Field(gt=0)
    utilization: float = Field(ge=0)
    latency_p50_ms: float = Field(gt=0)
    latency_p95_ms: float = Field(gt=0)
    error_rate: float = Field(ge=0, le=1)
    available_products: list[str]
    marginal_cost_micros: dict[str, StrictInt]


PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "customer.created": CustomerCreated,
    "usage.observed": UsageObserved,
    "request.completed": RequestCompleted,
    "subscription.started": SubscriptionStarted,
    "subscription.changed": SubscriptionChanged,
    "price.exposed": PriceExposed,
    "conversion.observed": ConversionObserved,
    "churn.observed": ChurnObserved,
    "invoice.created": InvoiceCreated,
    "payment.attempted": PaymentAttempted,
    "payment.failed": PaymentFailed,
    "payment.succeeded": PaymentSucceeded,
    "service.metric_observed": ServiceMetricObserved,
}


class UnknownEventType(ValueError):
    pass


def validate_payload(event_type: str, payload: dict[str, object]) -> BaseModel:
    try:
        model = PAYLOAD_MODELS[event_type]
    except KeyError:
        raise UnknownEventType(event_type) from None
    return model.model_validate(payload)
