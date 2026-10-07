"""Provider snapshot -> internal event contracts (pure, deterministic).

Both gateways go through these functions, so Stripe and the synthetic provider emit the
same ``invoice.created`` / ``payment.*`` / ``subscription.started`` / ``churn.observed``
events (ADR 0003, ADR 0014). Rules:

* Event ids are derived from provider object ids (``ids.derived_event_id``): the same state
  always produces the same events; new state only adds events.
* ``occurred_at`` comes from the provider's timestamps (charge created, invoice finalised,
  first payment, subscription end), never from the processing time.
* Attempt numbers are the order of the invoice's charges (created, id). Stripe's
  ``attempt_count`` is not used: it ignores manual retries.
* ``payment.failed.final`` is always ``false``. When a provider gives up is a dunning policy
  question (Phase 8); a failure never closes an invoice here.
* Drafts, zero-amount invoices and non-GBP invoices produce no events (reported via
  ``Derived.skipped``), because the internal contracts cannot represent them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from praxis.payments.ids import derived_event_id, flow_correlation_id
from praxis.payments.model import (
    ChargeStatus,
    CustomerProfile,
    InvoiceSnapshot,
    InvoiceStatus,
    Origin,
    SubscriptionSnapshot,
    SubscriptionStatus,
)

SOURCES = {"stripe": "stripe", "synthetic": "synthetic-gateway"}
BILLING_SOURCE = "praxis-billing"  # Praxis-side business events (enrolment)
SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class Derived:
    events: list[dict[str, Any]] = field(default_factory=list)
    skipped: str | None = None


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _source(provider: str) -> str:
    try:
        return SOURCES[provider]
    except KeyError:
        raise ValueError(f"unknown payment provider {provider!r}") from None


def _event(  # noqa: PLR0913 - an envelope has this many independent fields
    event_type: str,
    *,
    event_id: str,
    source: str,
    occurred_at: datetime,
    published_at: datetime,
    trace_id: str,
    correlation_id: str,
    entity_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": event_type,
        "source": source,
        "occurred_at": iso_utc(occurred_at),
        "published_at": iso_utc(published_at),
        "trace_id": trace_id,
        "correlation_id": correlation_id,
        "causation_id": None,
        "entity_id": entity_id,
        "is_synthetic": True,  # every Praxis customer is synthetic, Stripe sandbox included
        "payload": payload,
    }


def invoice_events(s: InvoiceSnapshot, *, published_at: datetime, trace_id: str) -> Derived:
    if s.status is InvoiceStatus.DRAFT:
        return Derived(skipped="draft")
    if s.amount_minor <= 0:
        return Derived(skipped="zero_amount")
    if s.currency != "GBP":
        return Derived(skipped="unsupported_currency")
    if s.tier is None:
        return Derived(skipped="no_tier")
    source = _source(s.provider)
    corr = flow_correlation_id(s.provider, "invoice", s.invoice_id)
    created_at = s.finalized_at or min((c.created_at for c in s.charges), default=None)
    if created_at is None:
        return Derived(skipped="not_finalized")

    def ev(event_type: str, role: str, when: datetime, payload: dict[str, Any]) -> dict[str, Any]:
        return _event(
            event_type,
            event_id=derived_event_id(s.provider, "invoice", s.invoice_id, role),
            source=source,
            occurred_at=when,
            published_at=published_at,
            trace_id=trace_id,
            correlation_id=corr,
            entity_id=s.customer_id,
            payload={"invoice_id": s.invoice_id, **payload},
        )

    money = {"amount_minor": s.amount_minor, "currency": "GBP"}
    events = [
        ev(
            "invoice.created",
            "created",
            created_at,
            {
                **money,
                "period_start": s.period_start.isoformat(),
                "period_end": s.period_end.isoformat(),
                "tier": s.tier,
            },
        )
    ]
    for number, charge in enumerate(
        sorted(s.charges, key=lambda c: (c.created_at, c.charge_id)), 1
    ):
        attempt = {"attempt_number": number, **money}
        events.append(
            ev(
                "payment.attempted",
                f"attempt-{number}",
                charge.created_at,
                {**attempt, "payment_method": charge.payment_method},
            )
        )
        if charge.status is ChargeStatus.SUCCEEDED:
            events.append(ev("payment.succeeded", f"result-{number}", charge.created_at, attempt))
        elif charge.status is ChargeStatus.FAILED:
            reason = charge.failure_reason or "card_declined"
            events.append(
                ev(
                    "payment.failed",
                    f"result-{number}",
                    charge.created_at,
                    {**attempt, "reason": reason, "final": False},
                )
            )
    return Derived(events)


def subscription_events(
    s: SubscriptionSnapshot, *, published_at: datetime, trace_id: str
) -> Derived:
    """``subscription.started`` once the first invoice is paid; ``churn.observed`` on end."""
    if s.activated_at is None:
        return Derived(skipped="not_activated")
    source = _source(s.provider)
    corr = flow_correlation_id(s.provider, "customer", s.customer_id)

    def ev(event_type: str, role: str, when: datetime, payload: dict[str, Any]) -> dict[str, Any]:
        return _event(
            event_type,
            event_id=derived_event_id(s.provider, "subscription", s.subscription_id, role),
            source=source,
            occurred_at=when,
            published_at=published_at,
            trace_id=trace_id,
            correlation_id=corr,
            entity_id=s.customer_id,
            payload=payload,
        )

    events = [
        ev(
            "subscription.started",
            "started",
            s.activated_at,
            {
                "tier": s.tier,
                "products": list(s.products),
                "origin": s.origin,
                "base_fee_minor": s.base_fee_minor,
                "billing_period_days": s.billing_period_days,
            },
        )
    ]
    if s.status is SubscriptionStatus.CANCELED and s.ended_at is not None:
        tenure = max(0, int((s.ended_at - s.activated_at).total_seconds() // SECONDS_PER_DAY))
        events.append(
            ev(
                "churn.observed",
                "ended",
                max(s.ended_at, s.activated_at),
                {"reason": s.cancellation_reason or "voluntary", "tenure_days": tenure},
            )
        )
    return Derived(events)


def enrolment_events(
    profile: CustomerProfile,
    *,
    origin: Origin,
    occurred_at: datetime,
    published_at: datetime,
    trace_id: str,
) -> list[dict[str, Any]]:
    """Praxis-side lifecycle facts that precede billing: the customer exists (and converted).

    ``occurred_at`` should be the provider customer's creation time, so these events sort
    before anything the provider later reports for the customer (test clocks included).
    """
    corr = flow_correlation_id("praxis", "customer", profile.customer_id)

    def ev(event_type: str, role: str, payload: dict[str, Any]) -> dict[str, Any]:
        return _event(
            event_type,
            event_id=derived_event_id("praxis", "customer", profile.customer_id, role),
            source=BILLING_SOURCE,
            occurred_at=occurred_at,
            published_at=published_at,
            trace_id=trace_id,
            correlation_id=corr,
            entity_id=profile.customer_id,
            payload=payload,
        )

    events = [
        ev(
            "customer.created",
            "created",
            {
                "region_id": profile.region_id,
                "industry": profile.industry,
                "tier": profile.tier,
                "preferred_payment_method": profile.preferred_payment_method,
                "is_existing": origin == "existing",
                "tenure_days": profile.tenure_days,
            },
        )
    ]
    if origin == "new":
        events.append(
            ev("conversion.observed", "converted", {"converted": True, "price_index_milli": 1000})
        )
    return events
