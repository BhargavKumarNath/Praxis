"""Billing workflows over any ``PaymentGateway`` (enrol, update payment method, recover, cancel).

The service owns the *intent* identities, so every provider write is keyed by the business
intent and a retried workflow is safe end to end:

* enrolment ``(customer, plan)``: provider customer (ref store + key), price (lookup key),
  payment method, subscription;
* payment-method change and invoice retry: a caller-supplied ``request_id`` names the intent
  (a retry of the same request reuses it; a new decision uses a new one).

It publishes only the Praxis-side facts that precede billing (``customer.created`` and, for a
new customer, ``conversion.observed``). Everything the provider decides (invoices, payments,
activation, cancellation) reaches the control plane through webhooks -> inbox -> processor,
for Stripe and the synthetic gateway alike.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from praxis.payments.gateway import PaymentGateway
from praxis.payments.ids import idempotency_key
from praxis.payments.model import (
    CustomerProfile,
    Notification,
    Origin,
    PaymentBehaviour,
    PaymentOutcome,
    Plan,
    SubscriptionRef,
)
from praxis.payments.normalise import enrolment_events
from praxis.payments.processor import EventPublisher
from praxis.payments.webhook import InboxWriter
from praxis.tracing import (
    current_correlation_id,
    current_trace_id,
    new_correlation_id,
    new_trace_id,
)


@dataclass(frozen=True)
class Enrolment:
    customer_id: str
    provider_customer_id: str
    price_id: str
    payment_method_id: str
    subscription: SubscriptionRef


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BillingService:
    def __init__(
        self,
        gateway: PaymentGateway,
        publisher: EventPublisher,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.gateway = gateway
        self._publisher = publisher
        self._clock = clock

    def enrol(
        self,
        profile: CustomerProfile,
        plan: Plan,
        behaviour: PaymentBehaviour,
        *,
        origin: Origin = "new",
        test_clock: str | None = None,
    ) -> Enrolment:
        if profile.tier != plan.tier:
            raise ValueError("the plan's tier must match the customer's tier")
        customer = self.gateway.ensure_customer(profile, test_clock=test_clock)
        self._publisher.publish(
            enrolment_events(
                profile,
                origin=origin,
                occurred_at=customer.created_at,
                published_at=self._clock(),
                trace_id=current_trace_id() or new_trace_id(),
            )
        )
        price_id = self.gateway.ensure_plan(plan)
        method_id = self.gateway.set_payment_method(
            customer.provider_id,
            behaviour,
            idempotency_key=idempotency_key("enrol_method", profile.customer_id, plan.lookup_key),
        )
        subscription = self.gateway.create_subscription(
            profile.customer_id,
            customer.provider_id,
            price_id,
            plan,
            origin,
            idempotency_key=idempotency_key("subscribe", profile.customer_id, plan.lookup_key),
        )
        return Enrolment(
            profile.customer_id, customer.provider_id, price_id, method_id, subscription
        )

    def update_payment_method(
        self, provider_customer_id: str, behaviour: PaymentBehaviour, *, request_id: str
    ) -> str:
        return self.gateway.set_payment_method(
            provider_customer_id,
            behaviour,
            idempotency_key=idempotency_key("update_method", provider_customer_id, request_id),
        )

    def retry_invoice(self, invoice_id: str, *, request_id: str) -> PaymentOutcome:
        """One recovery attempt (Phase 8 decides when; this only executes it, idempotently)."""
        return self.gateway.pay_invoice(
            invoice_id, idempotency_key=idempotency_key("retry_invoice", invoice_id, request_id)
        )

    def cancel(self, subscription_id: str) -> None:
        self.gateway.cancel_subscription(subscription_id)


def deliver_notifications(notifications: list[Notification], inbox: InboxWriter) -> int:
    """Synthetic provider -> the same durable inbox Stripe webhooks use. Returns new rows."""
    inserted = 0
    for n in notifications:
        body = json.dumps(
            [
                n.provider,
                n.event_id,
                n.event_type,
                n.kind.value,
                n.object_id,
                n.created_at.isoformat(),
            ]
        ).encode()
        inserted += inbox.record(
            n,
            body_sha256=hashlib.sha256(body).hexdigest(),
            trace_id=current_trace_id() or new_trace_id(),
            correlation_id=current_correlation_id() or new_correlation_id(),
        )
    return inserted
