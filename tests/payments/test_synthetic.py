"""``SyntheticPaymentGateway`` semantics (deterministic, Stripe-like) without the pipeline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from praxis.payments.gateway import ForeignObject, IdempotencyConflict
from praxis.payments.model import (
    ChargeStatus,
    InvoiceStatus,
    NotificationKind,
    OutcomeStatus,
    PaymentBehaviour,
    Plan,
    SubscriptionStatus,
)
from praxis.payments.synthetic import COLLECTION_DELAY, SyntheticPaymentGateway, add_month
from tests.payments.helpers import PLAN, T0, profile


def enrolled(
    behaviour: PaymentBehaviour, *, clock: bool = True
) -> tuple[SyntheticPaymentGateway, str | None, str, str]:
    gw = SyntheticPaymentGateway(start=T0)
    clock_id = gw.create_test_clock(T0, "t") if clock else None
    cus = gw.ensure_customer(profile("cust_a"), test_clock=clock_id)
    price = gw.ensure_plan(PLAN)
    gw.set_payment_method(cus.provider_id, behaviour, idempotency_key="pm-1")
    ref = gw.create_subscription(
        "cust_a", cus.provider_id, price, PLAN, "new", idempotency_key="sub-1"
    )
    assert ref.latest_invoice_id is not None
    return gw, clock_id, ref.subscription_id, ref.latest_invoice_id


def test_add_month_clamps_to_month_end() -> None:
    assert add_month(datetime(2026, 1, 31, tzinfo=UTC)) == datetime(2026, 2, 28, tzinfo=UTC)
    assert add_month(datetime(2026, 12, 15, tzinfo=UTC)) == datetime(2027, 1, 15, tzinfo=UTC)


def test_successful_first_payment_activates() -> None:
    gw, _, sub_id, inv_id = enrolled(PaymentBehaviour.SUCCEEDS)
    inv, sub = gw.fetch_invoice(inv_id), gw.fetch_subscription(sub_id)
    assert inv.status is InvoiceStatus.PAID and [c.status for c in inv.charges] == [
        ChargeStatus.SUCCEEDED
    ]
    assert sub.status is SubscriptionStatus.ACTIVE and sub.activated_at == T0
    assert (inv.customer_id, inv.tier, inv.amount_minor) == (
        "cust_a",
        "growth",
        PLAN.base_fee_minor,
    )
    kinds = {(n.kind, n.event_type) for n in gw.drain_notifications()}
    assert (NotificationKind.INVOICE, "invoice.paid") in kinds
    assert gw.drain_notifications() == []


@pytest.mark.parametrize(
    ("behaviour", "reason"),
    [
        (PaymentBehaviour.CHARGE_FAILS, "card_declined"),
        (PaymentBehaviour.INSUFFICIENT_FUNDS, "insufficient_funds"),
        (PaymentBehaviour.EXPIRED_CARD, "expired_card"),
        (PaymentBehaviour.PROCESSING_ERROR, "processor_error"),
    ],
)
def test_declines_leave_subscription_incomplete(behaviour: PaymentBehaviour, reason: str) -> None:
    gw, _, sub_id, inv_id = enrolled(behaviour)
    inv = gw.fetch_invoice(inv_id)
    assert inv.status is InvoiceStatus.OPEN and inv.charges[0].failure_reason == reason
    sub = gw.fetch_subscription(sub_id)
    assert sub.status is SubscriptionStatus.INCOMPLETE and sub.activated_at is None


def test_recovery_after_card_update() -> None:
    gw, _, sub_id, inv_id = enrolled(PaymentBehaviour.CHARGE_FAILS)
    assert gw.pay_invoice(inv_id, idempotency_key="pay-1").failure_reason == "card_declined"
    gw.set_payment_method("syn_cus_000001", PaymentBehaviour.SUCCEEDS, idempotency_key="pm-2")
    assert gw.pay_invoice(inv_id, idempotency_key="pay-2").status is OutcomeStatus.SUCCEEDED
    assert (
        gw.pay_invoice(inv_id, idempotency_key="pay-3").status is OutcomeStatus.SUCCEEDED
    )  # paid: no charge
    assert len(gw.fetch_invoice(inv_id).charges) == 3
    assert gw.fetch_subscription(sub_id).status is SubscriptionStatus.ACTIVE


def test_idempotency_replays_and_conflicts() -> None:
    gw, _, sub_id, inv_id = enrolled(PaymentBehaviour.CHARGE_FAILS)
    first = gw.pay_invoice(inv_id, idempotency_key="pay-1")
    assert gw.pay_invoice(inv_id, idempotency_key="pay-1") == first
    assert len(gw.fetch_invoice(inv_id).charges) == 2
    replay = gw.create_subscription(
        "cust_a", "syn_cus_000001", "syn_price_000001", PLAN, "new", idempotency_key="sub-1"
    )
    assert replay.subscription_id == sub_id
    with pytest.raises(IdempotencyConflict):
        gw.pay_invoice("syn_in_000001", idempotency_key="sub-1")


def test_renewal_is_collected_an_hour_after_the_period_end() -> None:
    gw, clock, sub_id, _ = enrolled(PaymentBehaviour.SUCCEEDS)
    assert clock is not None
    renewal_at = add_month(T0)
    gw.advance_test_clock(clock, renewal_at + timedelta(minutes=30))
    renewal = gw.fetch_invoice("syn_in_000002")
    assert renewal.status is InvoiceStatus.OPEN and renewal.charges == ()
    assert renewal.period_start == renewal_at.date()
    gw.advance_test_clock(clock, renewal_at + COLLECTION_DELAY)
    renewal = gw.fetch_invoice("syn_in_000002")
    assert (
        renewal.status is InvoiceStatus.PAID
        and renewal.charges[0].created_at == renewal_at + COLLECTION_DELAY
    )
    assert gw.fetch_subscription(sub_id).activated_at == T0


def test_failed_renewal_makes_the_subscription_past_due() -> None:
    gw, clock, sub_id, _ = enrolled(PaymentBehaviour.SUCCEEDS)
    assert clock is not None
    gw.set_payment_method(
        "syn_cus_000001", PaymentBehaviour.INSUFFICIENT_FUNDS, idempotency_key="pm-2"
    )
    gw.advance_test_clock(clock, add_month(add_month(T0)) + timedelta(hours=2))
    sub = gw.fetch_subscription(sub_id)
    assert sub.status is SubscriptionStatus.PAST_DUE
    assert [gw.fetch_invoice(f"syn_in_00000{i}").status for i in (1, 2, 3)] == [
        InvoiceStatus.PAID,
        InvoiceStatus.OPEN,
        InvoiceStatus.OPEN,
    ]


def test_cancel_is_idempotent_and_voluntary() -> None:
    gw, _, sub_id, _ = enrolled(PaymentBehaviour.SUCCEEDS, clock=False)
    gw.cancel_subscription(sub_id)
    gw.cancel_subscription(sub_id)
    snap = gw.fetch_subscription(sub_id)
    assert (snap.status, snap.ended_at, snap.cancellation_reason) == (
        SubscriptionStatus.CANCELED,
        T0,
        "voluntary",
    )
    assert gw.calls["cancel_subscription"] == 2


def test_paying_a_cancelled_subscriptions_invoice_keeps_it_cancelled() -> None:
    gw, _, sub_id, inv_id = enrolled(PaymentBehaviour.CHARGE_FAILS, clock=False)
    gw.cancel_subscription(sub_id)
    gw.set_payment_method("syn_cus_000001", PaymentBehaviour.SUCCEEDS, idempotency_key="pm-2")
    gw.pay_invoice(inv_id, idempotency_key="pay-1")
    assert gw.fetch_subscription(sub_id).status is SubscriptionStatus.CANCELED


def test_ensure_operations_are_idempotent() -> None:
    gw = SyntheticPaymentGateway(start=T0)
    assert gw.ensure_customer(profile("cust_a")) == gw.ensure_customer(profile("cust_a"))
    assert gw.ensure_plan(PLAN) == gw.ensure_plan(PLAN)
    other = Plan(tier="growth", products=("inference_api",), base_fee_minor=5900)
    assert gw.ensure_plan(other) != gw.ensure_plan(PLAN)


def test_unknown_objects_are_foreign() -> None:
    gw = SyntheticPaymentGateway(start=T0)
    with pytest.raises(ForeignObject):
        gw.ensure_customer(profile("cust_a"), test_clock="clock_missing")
    with pytest.raises(ForeignObject):
        gw.fetch_invoice("in_missing")
    with pytest.raises(ForeignObject):
        gw.fetch_subscription("sub_missing")
    with pytest.raises(ForeignObject):
        gw.set_payment_method("cus_missing", PaymentBehaviour.SUCCEEDS, idempotency_key="k")
    with pytest.raises(ForeignObject):
        gw.advance_test_clock("clock_missing", T0)
    cus = gw.ensure_customer(profile("cust_a"))
    with pytest.raises(ForeignObject):
        gw.create_subscription(
            "cust_other", cus.provider_id, "price_x", PLAN, "new", idempotency_key="k"
        )


def test_clock_rules() -> None:
    gw, clock, sub_id, _ = enrolled(PaymentBehaviour.SUCCEEDS)
    assert clock is not None
    with pytest.raises(ValueError, match="forward"):
        gw.advance_test_clock(clock, T0 - timedelta(seconds=1))
    gw.delete_test_clock(clock)
    assert gw.fetch_subscription(sub_id).status is SubscriptionStatus.CANCELED
    with pytest.raises(ValueError, match="timezone-aware"):
        SyntheticPaymentGateway(start=datetime(2026, 1, 1))  # noqa: DTZ001 - the point of the test


def test_no_payment_method_declines() -> None:
    gw = SyntheticPaymentGateway(start=T0)
    cus = gw.ensure_customer(profile("cust_a"))
    ref = gw.create_subscription(
        "cust_a", cus.provider_id, gw.ensure_plan(PLAN), PLAN, "new", idempotency_key="s"
    )
    assert ref.status is SubscriptionStatus.INCOMPLETE
    assert gw.provider == "synthetic"
