"""Snapshot -> internal events: contracts, determinism, and order independence at the fold."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from praxis.domain.projections import LoggedEvent, fold_customer, fold_invoice
from praxis.events.codec import validate_event
from praxis.payments.model import (
    ChargeAttempt,
    ChargeStatus,
    InvoiceSnapshot,
    InvoiceStatus,
    Origin,
    SubscriptionSnapshot,
    SubscriptionStatus,
)
from praxis.payments.normalise import (
    enrolment_events,
    invoice_events,
    iso_utc,
    subscription_events,
)
from tests.payments.helpers import T0, profile

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PUB = T0 + timedelta(minutes=5)


def charge(
    n: int, status: ChargeStatus, reason: Any = None, minutes: int | None = None
) -> ChargeAttempt:
    return ChargeAttempt(
        f"ch_{n:03d}",
        T0 + timedelta(minutes=n if minutes is None else minutes),
        status,
        "card",
        reason,
    )


def invoice(*charges: ChargeAttempt, **changes: Any) -> InvoiceSnapshot:
    base = InvoiceSnapshot(
        provider="stripe",
        invoice_id="in_001",
        customer_id="cust_a",
        status=InvoiceStatus.OPEN,
        amount_minor=4900,
        currency="GBP",
        period_start=date(2026, 10, 1),
        period_end=date(2026, 11, 1),
        finalized_at=T0,
        tier="growth",
        charges=charges,
    )
    return replace(base, **changes)


def subscription(**changes: Any) -> SubscriptionSnapshot:
    base = SubscriptionSnapshot(
        provider="stripe",
        subscription_id="sub_001",
        customer_id="cust_a",
        status=SubscriptionStatus.ACTIVE,
        tier="growth",
        products=("inference_api",),
        origin="new",
        base_fee_minor=4900,
        billing_period_days=30,
        activated_at=T0 + timedelta(minutes=1),
    )
    return replace(base, **changes)


def logged(events: list[dict[str, Any]]) -> list[LoggedEvent]:
    out = []
    for e in events:
        validate_event(e)  # every derived event satisfies the v1 envelope + payload contracts
        out.append(
            LoggedEvent(
                e["event_id"],
                e["event_type"],
                e["entity_id"],
                datetime.fromisoformat(e["occurred_at"]),
                e["payload"],
            )
        )
    return out


def test_declined_then_recovered_invoice() -> None:
    snap = invoice(
        charge(1, ChargeStatus.FAILED, "card_declined"), charge(2, ChargeStatus.SUCCEEDED)
    )
    events = invoice_events(snap, published_at=PUB, trace_id=TRACE).events
    assert [e["event_type"] for e in events] == [
        "invoice.created",
        "payment.attempted",
        "payment.failed",
        "payment.attempted",
        "payment.succeeded",
    ]
    failed = events[2]["payload"]
    assert failed == {
        "invoice_id": "in_001",
        "attempt_number": 1,
        "amount_minor": 4900,
        "currency": "GBP",
        "reason": "card_declined",
        "final": False,
    }
    proj = fold_invoice("in_001", logged(events))
    assert (proj.state, proj.attempts, proj.amount_paid_minor, proj.pending) == (
        "paid",
        2,
        4900,
        (),
    )
    assert {e["source"] for e in events} == {"stripe"}
    assert len({e["correlation_id"] for e in events}) == 1


def test_attempt_numbers_follow_charge_time_not_list_order() -> None:
    late_ok = charge(2, ChargeStatus.SUCCEEDED, minutes=30)
    early_fail = charge(9, ChargeStatus.FAILED, "insufficient_funds", minutes=10)
    events = invoice_events(invoice(late_ok, early_fail), published_at=PUB, trace_id=TRACE).events
    results = [
        (e["event_type"], e["payload"]["attempt_number"])
        for e in events[1:]
        if e["event_type"] != "payment.attempted"
    ]
    assert results == [("payment.failed", 1), ("payment.succeeded", 2)]


def test_pending_charge_is_an_attempt_without_result() -> None:
    events = invoice_events(
        invoice(charge(1, ChargeStatus.PENDING)), published_at=PUB, trace_id=TRACE
    ).events
    assert [e["event_type"] for e in events] == ["invoice.created", "payment.attempted"]
    assert fold_invoice("in_001", logged(events)).state == "attempting"


def test_failure_without_reason_defaults_to_card_declined() -> None:
    events = invoice_events(
        invoice(charge(1, ChargeStatus.FAILED)), published_at=PUB, trace_id=TRACE
    ).events
    assert events[-1]["payload"]["reason"] == "card_declined"


def test_derivation_is_deterministic_and_monotonic() -> None:
    one = invoice_events(
        invoice(charge(1, ChargeStatus.FAILED, "card_declined")), published_at=PUB, trace_id=TRACE
    )
    again = invoice_events(
        invoice(charge(1, ChargeStatus.FAILED, "card_declined")),
        published_at=PUB + timedelta(hours=1),
        trace_id="a" * 32,
    )
    later = invoice_events(
        invoice(charge(1, ChargeStatus.FAILED, "card_declined"), charge(2, ChargeStatus.SUCCEEDED)),
        published_at=PUB,
        trace_id=TRACE,
    )
    ids = [e["event_id"] for e in one.events]
    assert ids == [e["event_id"] for e in again.events]
    assert ids == [e["event_id"] for e in later.events][: len(ids)]  # new state only adds events
    assert len({e["event_id"] for e in later.events}) == len(later.events)


def test_providers_get_disjoint_event_ids() -> None:
    stripe = invoice_events(invoice(), published_at=PUB, trace_id=TRACE).events
    synthetic = invoice_events(
        invoice(provider="synthetic"), published_at=PUB, trace_id=TRACE
    ).events
    assert stripe[0]["event_id"] != synthetic[0]["event_id"]
    assert synthetic[0]["source"] == "synthetic-gateway"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"status": InvoiceStatus.DRAFT}, "draft"),
        ({"amount_minor": 0}, "zero_amount"),
        ({"currency": "USD"}, "unsupported_currency"),
        ({"tier": None}, "no_tier"),
        ({"finalized_at": None}, "not_finalized"),
    ],
)
def test_unrepresentable_invoices_are_skipped(changes: dict[str, Any], reason: str) -> None:
    derived = invoice_events(invoice(**changes), published_at=PUB, trace_id=TRACE)
    assert (derived.events, derived.skipped) == ([], reason)


def test_unfinalized_invoice_with_a_charge_uses_the_first_charge_time() -> None:
    derived = invoice_events(
        invoice(charge(3, ChargeStatus.SUCCEEDED), finalized_at=None),
        published_at=PUB,
        trace_id=TRACE,
    )
    assert derived.events[0]["occurred_at"] == iso_utc(T0 + timedelta(minutes=3))


def test_unknown_provider_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown payment provider"):
        invoice_events(invoice(provider="paypal"), published_at=PUB, trace_id=TRACE)


def test_iso_utc_requires_aware_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        iso_utc(datetime(2026, 1, 1))  # noqa: DTZ001 - the point of the test
    assert iso_utc(datetime(2026, 1, 1, 1, tzinfo=UTC)) == "2026-01-01T01:00:00Z"


# --- subscriptions + enrolment --------------------------------------------------------------------
def customer_events(sub: SubscriptionSnapshot, origin: Origin = "new") -> list[dict[str, Any]]:
    enrol = enrolment_events(
        profile("cust_a"), origin=origin, occurred_at=T0, published_at=PUB, trace_id=TRACE
    )
    return enrol + subscription_events(sub, published_at=PUB, trace_id=TRACE).events


def test_activated_subscription_makes_the_customer_active() -> None:
    proj = fold_customer("cust_a", logged(customer_events(subscription())))
    assert (proj.state, proj.subscription_state, proj.products) == (
        "active",
        "active",
        ("inference_api",),
    )
    assert proj.pending == ()


def test_cancelled_subscription_churns_with_tenure() -> None:
    ended = T0 + timedelta(days=40, minutes=2)
    events = customer_events(
        subscription(
            status=SubscriptionStatus.CANCELED,
            ended_at=ended,
            cancellation_reason="involuntary_payment",
        )
    )
    churn = events[-1]
    assert churn["event_type"] == "churn.observed"
    assert churn["payload"] == {"reason": "involuntary_payment", "tenure_days": 40}
    proj = fold_customer("cust_a", logged(events))
    assert (proj.state.value, proj.churn_reason) == ("churned", "involuntary_payment")  # type: ignore[union-attr]


def test_cancellation_without_reason_is_voluntary() -> None:
    events = customer_events(
        subscription(status=SubscriptionStatus.CANCELED, ended_at=T0 + timedelta(days=1))
    )
    assert events[-1]["payload"]["reason"] == "voluntary"


def test_never_activated_subscription_emits_nothing() -> None:
    derived = subscription_events(
        subscription(activated_at=None, status=SubscriptionStatus.INCOMPLETE),
        published_at=PUB,
        trace_id=TRACE,
    )
    assert (derived.events, derived.skipped) == ([], "not_activated")


def test_existing_customer_enrols_without_conversion() -> None:
    events = customer_events(subscription(origin="existing"), origin="existing")
    assert [e["event_type"] for e in events] == ["customer.created", "subscription.started"]
    assert events[0]["payload"]["is_existing"] is True and events[0]["source"] == "praxis-billing"
    assert fold_customer("cust_a", logged(events)).state == "active"


# --- order independence at the control-plane fold ------------------------------------------
INVOICE_EVENTS = invoice_events(
    invoice(
        charge(1, ChargeStatus.FAILED, "card_declined"),
        charge(2, ChargeStatus.FAILED, "insufficient_funds"),
        charge(3, ChargeStatus.SUCCEEDED),
    ),
    published_at=PUB,
    trace_id=TRACE,
).events
CUSTOMER_EVENTS = customer_events(
    subscription(status=SubscriptionStatus.CANCELED, ended_at=T0 + timedelta(days=60))
)


@settings(max_examples=150, deadline=None)
@given(st.data())
def test_any_delivery_order_with_duplicates_folds_to_the_same_state(data: st.DataObject) -> None:
    for events, fold, key in (
        (INVOICE_EVENTS, fold_invoice, "in_001"),
        (CUSTOMER_EVENTS, fold_customer, "cust_a"),
    ):
        reference = fold(key, logged(events))
        delivered = data.draw(
            st.permutations(events + data.draw(st.lists(st.sampled_from(events), max_size=6)))
        )
        observed = fold(key, logged(delivered))
        assert observed.state == reference.state
        assert observed.applied == reference.applied and observed.pending == ()
