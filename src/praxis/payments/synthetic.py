"""``SyntheticPaymentGateway``: a deterministic in-process payment provider.

Used for everything that must not touch Stripe (scale, chaos, CI) and as the reference
implementation of the shared contract suite. It models the subset of provider behaviour
Praxis relies on, with Stripe's semantics:

* subscriptions bill calendar months in advance; the first invoice is collected at creation
  (``allow_incomplete``: a decline leaves the subscription ``incomplete``, no exception);
* a renewal invoice is created at the period end and collected one hour later (Stripe waits
  an hour after ``invoice.created``); a failed renewal makes the subscription ``past_due``;
* paying an invoice (recovery) activates an ``incomplete`` / ``past_due`` subscription;
* every change emits a webhook-like ``Notification`` ("object X changed"), so the same inbox
  and processor path is exercised as for Stripe;
* writes honour idempotency keys (same key + same request = same result; same key + other
  request = ``IdempotencyConflict``);
* test clocks: objects of a customer created on a clock live on that clock's time.

Payment outcomes are a pure function of the payment method behaviour: no randomness, so a
run is reproducible bit for bit. Latency is not simulated yet (Phase 14 scale work).
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, TypeVar

from praxis.events.payloads import FailureReason, PaymentMethod
from praxis.payments.gateway import ForeignObject, IdempotencyConflict
from praxis.payments.model import (
    ChargeAttempt,
    ChargeStatus,
    CustomerProfile,
    InvoiceSnapshot,
    InvoiceStatus,
    Notification,
    NotificationKind,
    Origin,
    OutcomeStatus,
    PaymentBehaviour,
    PaymentOutcome,
    Plan,
    ProviderCustomer,
    SubscriptionRef,
    SubscriptionSnapshot,
    SubscriptionStatus,
)
from praxis.payments.store import MemoryRefStore, RefStore

PROVIDER = "synthetic"
COLLECTION_DELAY = timedelta(hours=1)
OUTCOMES: dict[PaymentBehaviour, FailureReason | None] = {
    PaymentBehaviour.SUCCEEDS: None,
    PaymentBehaviour.CHARGE_FAILS: "card_declined",
    PaymentBehaviour.INSUFFICIENT_FUNDS: "insufficient_funds",
    PaymentBehaviour.EXPIRED_CARD: "expired_card",
    PaymentBehaviour.PROCESSING_ERROR: "processor_error",
}
_LIVE = (SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE)
T = TypeVar("T")


def add_month(when: datetime) -> datetime:
    """Same day next month, clamped to the month's last day (calendar-month billing)."""
    year, month = (when.year + 1, 1) if when.month == 12 else (when.year, when.month + 1)
    day = min(when.day, calendar.monthrange(year, month)[1])
    return when.replace(year=year, month=month, day=day)


@dataclass
class _Customer:
    id: str
    praxis_id: str
    clock_id: str | None
    created_at: datetime
    default_method: PaymentBehaviour | None = None
    payment_method: PaymentMethod = "card"


@dataclass
class _Invoice:
    id: str
    customer: _Customer
    subscription_id: str
    plan: Plan
    period_start: datetime
    period_end: datetime
    finalized_at: datetime
    collect_at: datetime
    status: InvoiceStatus = InvoiceStatus.OPEN
    charges: list[ChargeAttempt] = field(default_factory=list)


@dataclass
class _Subscription:
    id: str
    customer: _Customer
    plan: Plan
    origin: Origin
    status: SubscriptionStatus
    period_start: datetime
    period_end: datetime
    activated_at: datetime | None = None
    ended_at: datetime | None = None


class SyntheticPaymentGateway:
    def __init__(self, *, start: datetime, refs: RefStore | None = None) -> None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        self._now = start.astimezone(UTC)
        self._refs = refs or MemoryRefStore()
        self._clocks: dict[str, datetime] = {}
        self._customers: dict[str, _Customer] = {}
        self._prices: dict[str, Plan] = {}
        self._subs: dict[str, _Subscription] = {}
        self._invoices: dict[str, _Invoice] = {}
        self._idempotency: dict[str, tuple[Hashable, Any]] = {}
        self._counters: dict[str, int] = {}
        self.notifications: list[Notification] = []
        self.calls: dict[str, int] = {}

    @property
    def provider(self) -> str:
        return PROVIDER

    # --- helpers -------------------------------------------------------------------------
    def _next_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"syn_{prefix}_{self._counters[prefix]:06d}"

    def _time(self, customer: _Customer) -> datetime:
        return self._clocks[customer.clock_id] if customer.clock_id else self._now

    def _notify(
        self, event_type: str, kind: NotificationKind, object_id: str, when: datetime
    ) -> None:
        self.notifications.append(
            Notification(PROVIDER, self._next_id("evt"), event_type, kind, object_id, when)
        )

    def _once(self, key: str, request: Hashable, action: Callable[[], T]) -> T:
        """Idempotent write: replay the stored result, refuse a different request."""
        if key in self._idempotency:
            stored_request, result = self._idempotency[key]
            if stored_request != request:
                raise IdempotencyConflict(f"key {key} reused for a different request")
            return result  # type: ignore[no-any-return]
        result = action()
        self._idempotency[key] = (request, result)
        return result

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def drain_notifications(self) -> list[Notification]:
        out, self.notifications = self.notifications, []
        return out

    # --- writes ----------------------------------------------------------------------------
    def ensure_customer(
        self, profile: CustomerProfile, *, test_clock: str | None = None
    ) -> ProviderCustomer:
        self._count("ensure_customer")
        existing = self._refs.get(PROVIDER, "customer", profile.customer_id)
        if existing is None:
            if test_clock is not None and test_clock not in self._clocks:
                raise ForeignObject(f"unknown test clock {test_clock}")
            cid = self._next_id("cus")
            now = self._clocks[test_clock] if test_clock else self._now
            self._customers[cid] = _Customer(
                cid,
                profile.customer_id,
                test_clock,
                now,
                payment_method=profile.preferred_payment_method,
            )
            self._refs.put(PROVIDER, "customer", profile.customer_id, cid)
            existing = cid
        customer = self._customers[existing]
        return ProviderCustomer(customer.id, customer.created_at)

    def ensure_plan(self, plan: Plan) -> str:
        self._count("ensure_plan")
        existing = self._refs.get(PROVIDER, "price", plan.lookup_key)
        if existing is None:
            existing = self._next_id("price")
            self._prices[existing] = plan
            self._refs.put(PROVIDER, "price", plan.lookup_key, existing)
        return existing

    def set_payment_method(
        self, provider_customer_id: str, behaviour: PaymentBehaviour, *, idempotency_key: str
    ) -> str:
        self._count("set_payment_method")
        customer = self._customer(provider_customer_id)

        def attach() -> str:
            customer.default_method = behaviour
            return self._next_id("pm")

        return self._once(idempotency_key, ("pm", provider_customer_id, behaviour), attach)

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
        self._count("create_subscription")
        customer = self._customer(provider_customer_id)
        if customer.praxis_id != customer_id or self._prices.get(price_id) != plan:
            raise ForeignObject("customer or price does not match the request")

        def create() -> SubscriptionRef:
            now = self._time(customer)
            sub = _Subscription(
                self._next_id("sub"),
                customer,
                plan,
                origin,
                SubscriptionStatus.INCOMPLETE,
                now,
                add_month(now),
            )
            self._subs[sub.id] = sub
            self._notify("subscription.created", NotificationKind.SUBSCRIPTION, sub.id, now)
            invoice = self._new_invoice(sub, now, collect_at=now)
            self._collect(invoice, now)
            return SubscriptionRef(sub.id, sub.status, invoice.id)

        request = ("sub", customer_id, provider_customer_id, price_id, plan, origin)
        return self._once(idempotency_key, request, create)

    def pay_invoice(self, invoice_id: str, *, idempotency_key: str) -> PaymentOutcome:
        self._count("pay_invoice")
        invoice = self._invoice(invoice_id)

        def pay() -> PaymentOutcome:
            if invoice.status is InvoiceStatus.PAID:
                return PaymentOutcome(OutcomeStatus.SUCCEEDED)
            charge = self._collect(invoice, self._time(invoice.customer))
            if charge.status is ChargeStatus.SUCCEEDED:
                return PaymentOutcome(OutcomeStatus.SUCCEEDED)
            return PaymentOutcome(OutcomeStatus.FAILED, charge.failure_reason)

        return self._once(idempotency_key, ("pay", invoice_id), pay)

    def cancel_subscription(self, subscription_id: str) -> None:
        self._count("cancel_subscription")
        sub = self._subscription(subscription_id)
        if sub.status is SubscriptionStatus.CANCELED:
            return
        now = self._time(sub.customer)
        sub.status, sub.ended_at = SubscriptionStatus.CANCELED, now
        self._notify("subscription.deleted", NotificationKind.SUBSCRIPTION, sub.id, now)

    # --- billing mechanics -------------------------------------------------------------------
    def _new_invoice(self, sub: _Subscription, now: datetime, *, collect_at: datetime) -> _Invoice:
        invoice = _Invoice(
            self._next_id("in"),
            sub.customer,
            sub.id,
            sub.plan,
            sub.period_start,
            sub.period_end,
            now,
            collect_at,
        )
        self._invoices[invoice.id] = invoice
        self._notify("invoice.finalized", NotificationKind.INVOICE, invoice.id, now)
        return invoice

    def _collect(self, invoice: _Invoice, now: datetime) -> ChargeAttempt:
        behaviour = invoice.customer.default_method
        reason = OUTCOMES[behaviour] if behaviour is not None else "card_declined"
        status = ChargeStatus.FAILED if reason else ChargeStatus.SUCCEEDED
        charge = ChargeAttempt(
            self._next_id("ch"), now, status, invoice.customer.payment_method, reason
        )
        invoice.charges.append(charge)
        sub = self._subs[invoice.subscription_id]
        if status is ChargeStatus.SUCCEEDED:
            invoice.status = InvoiceStatus.PAID
            if sub.status is not SubscriptionStatus.CANCELED:
                sub.status = SubscriptionStatus.ACTIVE
            sub.activated_at = sub.activated_at or now
            self._notify("invoice.paid", NotificationKind.INVOICE, invoice.id, now)
        else:
            if sub.status is SubscriptionStatus.ACTIVE:
                sub.status = SubscriptionStatus.PAST_DUE
            self._notify("invoice.payment_failed", NotificationKind.INVOICE, invoice.id, now)
        self._notify("subscription.updated", NotificationKind.SUBSCRIPTION, sub.id, now)
        return charge

    # --- reads -----------------------------------------------------------------------------
    def _customer(self, provider_customer_id: str) -> _Customer:
        try:
            return self._customers[provider_customer_id]
        except KeyError:
            raise ForeignObject(f"unknown customer {provider_customer_id}") from None

    def _invoice(self, invoice_id: str) -> _Invoice:
        try:
            return self._invoices[invoice_id]
        except KeyError:
            raise ForeignObject(f"unknown invoice {invoice_id}") from None

    def _subscription(self, subscription_id: str) -> _Subscription:
        try:
            return self._subs[subscription_id]
        except KeyError:
            raise ForeignObject(f"unknown subscription {subscription_id}") from None

    def fetch_invoice(self, invoice_id: str) -> InvoiceSnapshot:
        self._count("fetch_invoice")
        inv = self._invoice(invoice_id)
        return InvoiceSnapshot(
            provider=PROVIDER,
            invoice_id=inv.id,
            customer_id=inv.customer.praxis_id,
            status=inv.status,
            amount_minor=inv.plan.base_fee_minor,
            currency=inv.plan.currency,
            period_start=inv.period_start.date(),
            period_end=inv.period_end.date(),
            finalized_at=inv.finalized_at,
            tier=inv.plan.tier,
            charges=tuple(inv.charges),
        )

    def fetch_subscription(self, subscription_id: str) -> SubscriptionSnapshot:
        self._count("fetch_subscription")
        sub = self._subscription(subscription_id)
        return SubscriptionSnapshot(
            provider=PROVIDER,
            subscription_id=sub.id,
            customer_id=sub.customer.praxis_id,
            status=sub.status,
            tier=sub.plan.tier,
            products=sub.plan.products,
            origin=sub.origin,
            base_fee_minor=sub.plan.base_fee_minor,
            billing_period_days=sub.plan.billing_period_days,
            activated_at=sub.activated_at,
            ended_at=sub.ended_at,
            cancellation_reason="voluntary" if sub.status is SubscriptionStatus.CANCELED else None,
        )

    # --- test clocks -------------------------------------------------------------------------
    def create_test_clock(self, frozen_time: datetime, name: str) -> str:
        clock_id = self._next_id("clock")
        self._clocks[clock_id] = frozen_time.astimezone(UTC)
        return clock_id

    def advance_test_clock(self, clock_id: str, to: datetime) -> None:
        if clock_id not in self._clocks:
            raise ForeignObject(f"unknown test clock {clock_id}")
        target = to.astimezone(UTC)
        if target < self._clocks[clock_id]:
            raise ValueError("a test clock only moves forward")
        while True:  # one billing event at a time, in time order
            step = self._next_billing_event(clock_id, target)
            if step is None:
                break
            when, action = step
            self._clocks[clock_id] = when
            action()
        self._clocks[clock_id] = target

    def _next_billing_event(
        self, clock_id: str, target: datetime
    ) -> tuple[datetime, Callable[[], None]] | None:
        candidates: list[tuple[datetime, str, Callable[[], None]]] = []
        for sub in self._subs.values():
            if (
                sub.customer.clock_id == clock_id
                and sub.status in _LIVE
                and sub.period_end <= target
            ):
                candidates.append((sub.period_end, sub.id, partial(self._renew, sub)))
        for inv in self._invoices.values():
            due = inv.customer.clock_id == clock_id and inv.status is InvoiceStatus.OPEN
            if due and not inv.charges and inv.collect_at <= target:
                candidates.append((inv.collect_at, inv.id, partial(self._collect_due, inv)))
        if not candidates:
            return None
        when, _, action = min(candidates, key=lambda c: (c[0], c[1]))
        return when, action

    def _collect_due(self, invoice: _Invoice) -> None:
        self._collect(invoice, invoice.collect_at)

    def _renew(self, sub: _Subscription) -> None:
        sub.period_start, sub.period_end = sub.period_end, add_month(sub.period_end)
        self._new_invoice(sub, sub.period_start, collect_at=sub.period_start + COLLECTION_DELAY)

    def delete_test_clock(self, clock_id: str) -> None:
        for sub in self._subs.values():
            if sub.customer.clock_id == clock_id:
                sub.status = SubscriptionStatus.CANCELED
        self._clocks.pop(clock_id, None)
