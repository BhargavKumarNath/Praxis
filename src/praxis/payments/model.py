"""Provider-neutral payment model shared by every ``PaymentGateway``.

Snapshots describe the provider's *current* state of one object. The processor never
interprets a webhook payload: a webhook only says "object X changed", the gateway fetches X,
and ``praxis.payments.normalise`` derives internal events from the snapshot. Deriving from
state (not from deliveries) is what makes duplicates and reordering harmless.

Money is integer minor units; timestamps are timezone-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from praxis.events.payloads import FailureReason, PaymentMethod, Tier

Origin = Literal["new", "existing"]
ChurnReason = Literal["voluntary", "involuntary_payment"]


class PaymentBehaviour(StrEnum):
    """Deterministic payment-method behaviours. Stripe supports the first two (test cards)."""

    SUCCEEDS = "succeeds"
    CHARGE_FAILS = "charge_fails"  # attaches, every charge is declined (card_declined)
    INSUFFICIENT_FUNDS = "insufficient_funds"
    EXPIRED_CARD = "expired_card"
    PROCESSING_ERROR = "processing_error"


class ChargeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PENDING = "pending"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    PAID = "paid"
    UNCOLLECTIBLE = "uncollectible"
    VOID = "void"


class SubscriptionStatus(StrEnum):
    INCOMPLETE = "incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired"
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    UNPAID = "unpaid"
    PAUSED = "paused"
    CANCELED = "canceled"


class NotificationKind(StrEnum):
    INVOICE = "invoice"
    SUBSCRIPTION = "subscription"


class OutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class CustomerProfile:
    """The Praxis customer being billed. Only synthetic identifiers leave Praxis."""

    customer_id: str
    region_id: str
    industry: str
    tier: Tier
    preferred_payment_method: PaymentMethod = "card"
    tenure_days: int = 0  # > 0 only for pre-existing customers (origin "existing")


@dataclass(frozen=True)
class Plan:
    tier: Tier
    products: tuple[str, ...]
    base_fee_minor: int
    currency: Literal["GBP"] = "GBP"
    interval: Literal["month"] = "month"
    # Nominal period for the internal contract; the provider bills calendar months.
    billing_period_days: int = 30

    def __post_init__(self) -> None:
        if self.base_fee_minor <= 0:
            raise ValueError("base_fee_minor must be positive")
        if not self.products:
            raise ValueError("a plan needs at least one product")

    @property
    def lookup_key(self) -> str:
        """Stable price identity: a new fee is a new price, never an edit of an old one."""
        return f"praxis_{self.tier}_{self.currency.lower()}_{self.base_fee_minor}_{self.interval}"

    @property
    def product_key(self) -> str:
        return f"praxis_{self.tier}"


@dataclass(frozen=True)
class ProviderCustomer:
    provider_id: str
    created_at: datetime


@dataclass(frozen=True)
class SubscriptionRef:
    subscription_id: str
    status: SubscriptionStatus
    latest_invoice_id: str | None


@dataclass(frozen=True)
class PaymentOutcome:
    status: OutcomeStatus
    failure_reason: FailureReason | None = None


@dataclass(frozen=True)
class ChargeAttempt:
    """One attempt to collect an invoice (a provider charge), whoever triggered it."""

    charge_id: str
    created_at: datetime
    status: ChargeStatus
    payment_method: PaymentMethod
    failure_reason: FailureReason | None = None


@dataclass(frozen=True)
class InvoiceSnapshot:
    provider: str
    invoice_id: str
    customer_id: str  # Praxis customer id
    status: InvoiceStatus
    amount_minor: int
    currency: str  # upper-case ISO code
    period_start: date
    period_end: date
    finalized_at: datetime | None
    tier: Tier | None
    charges: tuple[ChargeAttempt, ...] = field(default=())


@dataclass(frozen=True)
class SubscriptionSnapshot:
    provider: str
    subscription_id: str
    customer_id: str  # Praxis customer id
    status: SubscriptionStatus
    tier: Tier
    products: tuple[str, ...]
    origin: Origin
    base_fee_minor: int
    billing_period_days: int
    activated_at: datetime | None  # first time an invoice of this subscription was paid
    ended_at: datetime | None = None
    cancellation_reason: ChurnReason | None = None


@dataclass(frozen=True)
class Notification:
    """A verified provider event, reduced to "this object changed"."""

    provider: str
    event_id: str
    event_type: str
    kind: NotificationKind
    object_id: str
    created_at: datetime
    livemode: bool = False
    api_version: str | None = None
