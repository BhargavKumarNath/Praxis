"""The ``PaymentGateway`` contract (ADR 0003) and its failure taxonomy.

Every write takes an ``idempotency_key`` derived from the business intent
(``praxis.payments.ids.idempotency_key``), so a retried call after a crash or timeout can
never create a second customer, subscription or charge. ``ensure_*`` operations are
idempotent beyond the provider's key window through the provider-ref store.

Declines are not exceptions: ``pay_invoice`` returns a failed ``PaymentOutcome``. Exceptions
are reserved for the transport and the contract:

* ``TransientError`` (from ``praxis.errors``): may succeed on retry (timeouts, 429, 5xx);
* ``GatewayError`` (permanent): bad request, auth, conflicting idempotency key;
* ``UnsupportedOperation``: the backend cannot express this (e.g. Stripe cannot attach a
  card that simulates insufficient funds); the shared contract suite skips explicitly;
* ``ForeignObject``: the provider object was not created by Praxis (no mapping, no
  metadata); the processor records it as ignored instead of guessing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from praxis.errors import PermanentError
from praxis.payments.model import (
    CustomerProfile,
    InvoiceSnapshot,
    Origin,
    PaymentBehaviour,
    PaymentOutcome,
    Plan,
    ProviderCustomer,
    SubscriptionRef,
    SubscriptionSnapshot,
)


class GatewayError(PermanentError):
    """The provider refused the request and retrying it unchanged cannot help."""


class IdempotencyConflict(GatewayError):
    def __init__(self, detail: str) -> None:
        super().__init__("idempotency_conflict", detail)


class UnsupportedOperation(GatewayError):
    def __init__(self, detail: str) -> None:
        super().__init__("unsupported_operation", detail)


class ForeignObject(GatewayError):
    def __init__(self, detail: str) -> None:
        super().__init__("foreign_object", detail)


class SnapshotSource(Protocol):
    """Read side used by the asynchronous processor."""

    @property
    def provider(self) -> str: ...

    def fetch_invoice(self, invoice_id: str) -> InvoiceSnapshot: ...

    def fetch_subscription(self, subscription_id: str) -> SubscriptionSnapshot: ...


class PaymentGateway(SnapshotSource, Protocol):
    def ensure_customer(
        self, profile: CustomerProfile, *, test_clock: str | None = None
    ) -> ProviderCustomer:
        """Create the provider customer once per Praxis customer; later calls return it."""
        ...

    def ensure_plan(self, plan: Plan) -> str:
        """Create the product and price once per ``plan.lookup_key``; returns the price id."""
        ...

    def set_payment_method(
        self, provider_customer_id: str, behaviour: PaymentBehaviour, *, idempotency_key: str
    ) -> str:
        """Attach a payment method and make it the customer's default; returns its id."""
        ...

    def create_subscription(
        self,
        customer_id: str,
        provider_customer_id: str,
        price_id: str,
        plan: Plan,
        origin: Origin,
        *,
        idempotency_key: str,
    ) -> SubscriptionRef:
        """Start a subscription and attempt its first invoice; a decline leaves it incomplete."""
        ...

    def pay_invoice(self, invoice_id: str, *, idempotency_key: str) -> PaymentOutcome:
        """Attempt collection now with the customer's default payment method."""
        ...

    def cancel_subscription(self, subscription_id: str) -> None:
        """Cancel immediately (voluntary). Cancelling a cancelled subscription is a no-op."""
        ...


class ClockControl(Protocol):
    """Deterministic time travel (Stripe Test Clocks; the synthetic gateway's own clock)."""

    def create_test_clock(self, frozen_time: datetime, name: str) -> str: ...

    def advance_test_clock(self, clock_id: str, to: datetime) -> None:
        """Advance and return once the provider has finished processing the new time."""
        ...

    def delete_test_clock(self, clock_id: str) -> None:
        """Delete the clock and every object attached to it."""
        ...
