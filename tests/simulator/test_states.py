"""State tests: every valid transition passes, every forbidden transition fails."""

from __future__ import annotations

from itertools import product
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from praxis.domain.states import (
    CUSTOMER_TRANSITIONS,
    INVOICE_TRANSITIONS,
    CustomerState,
    InvalidTransition,
    InvoiceState,
)
from praxis.simulator.events import Draft, EventFactory
from praxis.simulator.validation import StreamValidator, StreamViolation

CUSTOMER_ALLOWED = {
    (CustomerState.PROSPECT, "conversion.converted"): CustomerState.CONVERTED,
    (CustomerState.PROSPECT, "conversion.not_converted"): CustomerState.LOST,
    (CustomerState.CONVERTED, "subscription.started"): CustomerState.ACTIVE,
    (CustomerState.PROSPECT, "subscription.started"): CustomerState.ACTIVE,
    (CustomerState.ACTIVE, "subscription.changed"): CustomerState.ACTIVE,
    (CustomerState.ACTIVE, "churn.observed"): CustomerState.CHURNED,
}
INVOICE_ALLOWED = {
    (InvoiceState.OPEN, "payment.attempted"): InvoiceState.ATTEMPTING,
    (InvoiceState.ATTEMPTING, "payment.failed.retry"): InvoiceState.OPEN,
    (InvoiceState.ATTEMPTING, "payment.failed.final"): InvoiceState.UNCOLLECTIBLE,
    (InvoiceState.ATTEMPTING, "payment.succeeded"): InvoiceState.PAID,
}


@pytest.mark.parametrize(("key", "expected"), list(CUSTOMER_ALLOWED.items()))
def test_every_allowed_customer_transition(
    key: tuple[CustomerState, str], expected: CustomerState
) -> None:
    assert CUSTOMER_TRANSITIONS.apply(*key) is expected


def test_every_forbidden_customer_transition_fails() -> None:
    for state, trigger in product(CustomerState, CUSTOMER_TRANSITIONS.triggers):
        if (state, trigger) in CUSTOMER_ALLOWED:
            continue
        with pytest.raises(InvalidTransition):
            CUSTOMER_TRANSITIONS.apply(state, trigger)


@pytest.mark.parametrize(("key", "expected"), list(INVOICE_ALLOWED.items()))
def test_every_allowed_invoice_transition(
    key: tuple[InvoiceState, str], expected: InvoiceState
) -> None:
    assert INVOICE_TRANSITIONS.apply(*key) is expected


def test_every_forbidden_invoice_transition_fails() -> None:
    for state, trigger in product(InvoiceState, INVOICE_TRANSITIONS.triggers):
        if (state, trigger) in INVOICE_ALLOWED:
            continue
        with pytest.raises(InvalidTransition):
            INVOICE_TRANSITIONS.apply(state, trigger)


def test_entry_points() -> None:
    assert CUSTOMER_TRANSITIONS.start("customer.created") is CustomerState.PROSPECT
    assert INVOICE_TRANSITIONS.start("invoice.created") is InvoiceState.OPEN
    with pytest.raises(InvalidTransition):
        CUSTOMER_TRANSITIONS.start("usage.observed")
    assert CUSTOMER_TRANSITIONS.entries() == {"customer.created"}


def test_terminal_states_are_absorbing() -> None:
    for terminal in (CustomerState.CHURNED, CustomerState.LOST):
        assert CUSTOMER_TRANSITIONS.allowed(terminal) == frozenset()
    for terminal_i in (InvoiceState.PAID, InvoiceState.UNCOLLECTIBLE):
        assert INVOICE_TRANSITIONS.allowed(terminal_i) == frozenset()


@given(st.lists(st.sampled_from(sorted(CUSTOMER_TRANSITIONS.triggers)), max_size=25))
def test_property_customer_random_walk_never_reaches_an_impossible_state(
    triggers: list[str],
) -> None:
    state = CustomerState.PROSPECT
    for trig in triggers:
        allowed = trig in CUSTOMER_TRANSITIONS.allowed(state)
        try:
            state = CUSTOMER_TRANSITIONS.apply(state, trig)
        except InvalidTransition:
            assert not allowed
        else:
            assert allowed
        assert state in set(CustomerState)


@given(
    st.lists(
        st.sampled_from(sorted(INVOICE_TRANSITIONS.triggers - {"invoice.created"})), max_size=25
    )
)
def test_property_invoice_random_walk_never_reaches_an_impossible_state(
    triggers: list[str],
) -> None:
    state = InvoiceState.OPEN
    for trig in triggers:
        allowed = trig in INVOICE_TRANSITIONS.allowed(state)
        try:
            state = INVOICE_TRANSITIONS.apply(state, trig)
        except InvalidTransition:
            assert not allowed
        else:
            assert allowed


# ---------------------------------------------------------------- stream validator ----
FACTORY = EventFactory("test-run")
T0 = 1_767_571_200  # 2026-01-05T00:00:00Z


def ev(
    ts_off: int,
    etype: str,
    entity: str,
    payload: dict[str, Any],
    uid: str,
    cause: str | None = None,
) -> dict[str, Any]:
    return FACTORY.build(Draft(T0 + ts_off, entity, uid, etype, payload, f"f:{entity}", cause))


CREATED = {
    "region_id": "eu_west",
    "industry": "saas",
    "tier": "growth",
    "preferred_payment_method": "card",
    "is_existing": True,
    "tenure_days": 5,
}
SUB = {
    "tier": "growth",
    "products": ["api_requests"],
    "origin": "existing",
    "base_fee_minor": 9900,
    "billing_period_days": 30,
}
USAGE = {
    "product": "api_requests",
    "region_id": "eu_west",
    "units": 5,
    "throttled_units": 0,
    "unit_price_micros": 400000,
}
INV = {
    "invoice_id": "inv_1",
    "amount_minor": 1000,
    "currency": "GBP",
    "period_start": "2026-01-01",
    "period_end": "2026-01-30",
    "tier": "growth",
}
ATT = {
    "invoice_id": "inv_1",
    "attempt_number": 1,
    "amount_minor": 1000,
    "currency": "GBP",
    "payment_method": "card",
}


def feed_all(events: list[dict[str, Any]], max_attempts: int = 3) -> None:
    v = StreamValidator(max_attempts)
    for e in events:
        v.feed(e)


def active_prefix() -> list[dict[str, Any]]:
    return [
        ev(1, "customer.created", "c1", CREATED, "c"),
        ev(2, "subscription.started", "c1", SUB, "s"),
    ]


def test_validator_accepts_a_legal_stream() -> None:
    feed_all(
        [
            *active_prefix(),
            ev(3, "usage.observed", "c1", USAGE, "u"),
            ev(4, "invoice.created", "c1", INV, "i"),
            ev(5, "payment.attempted", "c1", ATT, "a", "i"),
            ev(
                6,
                "payment.succeeded",
                "c1",
                {k: ATT[k] for k in ("invoice_id", "attempt_number", "amount_minor", "currency")},
                "p",
                "a",
            ),
        ]
    )


@pytest.mark.parametrize(
    ("name", "events"),
    [
        ("usage_before_created", lambda: [ev(1, "usage.observed", "c1", USAGE, "u")]),
        (
            "usage_while_prospect",
            lambda: [
                ev(1, "customer.created", "c1", CREATED, "c"),
                ev(2, "usage.observed", "c1", USAGE, "u"),
            ],
        ),
        (
            "double_created",
            lambda: [
                ev(1, "customer.created", "c1", CREATED, "c"),
                ev(2, "customer.created", "c1", CREATED, "c2"),
            ],
        ),
        (
            "new_origin_without_conversion",
            lambda: [
                ev(1, "customer.created", "c1", CREATED, "c"),
                ev(2, "subscription.started", "c1", {**SUB, "origin": "new"}, "s"),
            ],
        ),
        (
            "usage_after_churn",
            lambda: [
                *active_prefix(),
                ev(3, "churn.observed", "c1", {"reason": "voluntary", "tenure_days": 6}, "x"),
                ev(4, "usage.observed", "c1", USAGE, "u"),
            ],
        ),
        (
            "double_churn",
            lambda: [
                *active_prefix(),
                ev(3, "churn.observed", "c1", {"reason": "voluntary", "tenure_days": 6}, "x"),
                ev(4, "churn.observed", "c1", {"reason": "voluntary", "tenure_days": 6}, "y"),
            ],
        ),
        (
            "change_to_same_tier",
            lambda: [
                *active_prefix(),
                ev(
                    3,
                    "subscription.changed",
                    "c1",
                    {"from_tier": "growth", "to_tier": "growth", "base_fee_minor": 9900},
                    "g",
                ),
            ],
        ),
        (
            "change_wrong_from_tier",
            lambda: [
                *active_prefix(),
                ev(
                    3,
                    "subscription.changed",
                    "c1",
                    {"from_tier": "starter", "to_tier": "growth", "base_fee_minor": 9900},
                    "g",
                ),
            ],
        ),
        (
            "timestamp_backwards_for_entity",
            lambda: [
                *active_prefix(),
                ev(9, "usage.observed", "c1", USAGE, "u"),
                ev(8, "usage.observed", "c1", {**USAGE, "units": 6}, "u2"),
            ],
        ),
        (
            "duplicate_event_id",
            lambda: [
                *active_prefix(),
                ev(3, "usage.observed", "c1", USAGE, "u"),
                ev(3, "usage.observed", "c1", USAGE, "u"),
            ],
        ),
        (
            "dangling_causation",
            lambda: [
                *active_prefix(),
                ev(3, "usage.observed", "c1", USAGE, "u", cause="never-emitted"),
            ],
        ),
        (
            "payment_without_invoice",
            lambda: [*active_prefix(), ev(3, "payment.attempted", "c1", ATT, "a")],
        ),
        (
            "success_without_attempt",
            lambda: [
                *active_prefix(),
                ev(3, "invoice.created", "c1", INV, "i"),
                ev(
                    4,
                    "payment.succeeded",
                    "c1",
                    {
                        k: ATT[k]
                        for k in ("invoice_id", "attempt_number", "amount_minor", "currency")
                    },
                    "p",
                ),
            ],
        ),
        (
            "attempt_number_skips",
            lambda: [
                *active_prefix(),
                ev(3, "invoice.created", "c1", INV, "i"),
                ev(4, "payment.attempted", "c1", {**ATT, "attempt_number": 2}, "a"),
            ],
        ),
        (
            "amount_mismatch",
            lambda: [
                *active_prefix(),
                ev(3, "invoice.created", "c1", INV, "i"),
                ev(4, "payment.attempted", "c1", {**ATT, "amount_minor": 999}, "a"),
            ],
        ),
        (
            "negative_units_rejected_by_schema",
            lambda: [*active_prefix(), ev(3, "usage.observed", "c1", {**USAGE, "units": -1}, "u")],
        ),
        ("unknown_event_type", lambda: [ev(1, "customer.exploded", "c1", {}, "z")]),
        (
            "final_flag_wrong",
            lambda: [
                *active_prefix(),
                ev(3, "invoice.created", "c1", INV, "i"),
                ev(4, "payment.attempted", "c1", ATT, "a", "i"),
                ev(
                    5,
                    "payment.failed",
                    "c1",
                    {
                        **{
                            k: ATT[k]
                            for k in ("invoice_id", "attempt_number", "amount_minor", "currency")
                        },
                        "reason": "card_declined",
                        "final": True,
                    },
                    "f",
                    "a",
                ),
            ],
        ),
    ],
)
def test_validator_rejects_impossible_streams(name: str, events: Any) -> None:
    with pytest.raises(StreamViolation):
        feed_all(events())


def test_payment_after_churn_may_complete_settlement() -> None:
    feed_all(
        [
            *active_prefix(),
            ev(3, "invoice.created", "c1", INV, "i"),
            ev(4, "churn.observed", "c1", {"reason": "voluntary", "tenure_days": 6}, "x"),
            ev(5, "payment.attempted", "c1", ATT, "a", "i"),
        ]
    )


# ---- additional validator branches ---------------------------------------------------
RESULT = {k: ATT[k] for k in ("invoice_id", "attempt_number", "amount_minor", "currency")}


def invoiced() -> list[dict[str, Any]]:
    return [*active_prefix(), ev(3, "invoice.created", "c1", INV, "i")]


@pytest.mark.parametrize(
    ("name", "events"),
    [
        (
            "subscription_tier_differs_from_customer",
            lambda: [
                ev(1, "customer.created", "c1", CREATED, "c"),
                ev(2, "subscription.started", "c1", {**SUB, "tier": "starter"}, "s"),
            ],
        ),
        ("duplicate_invoice", lambda: [*invoiced(), ev(4, "invoice.created", "c1", INV, "i2")]),
        (
            "invoice_owned_by_other_customer",
            lambda: [
                *invoiced(),
                ev(4, "customer.created", "c2", CREATED, "c2"),
                ev(5, "subscription.started", "c2", SUB, "s2"),
                ev(6, "payment.attempted", "c2", ATT, "a"),
            ],
        ),
        (
            "attempt_while_attempting",
            lambda: [
                *invoiced(),
                ev(4, "payment.attempted", "c1", ATT, "a", "i"),
                ev(5, "payment.attempted", "c1", {**ATT, "attempt_number": 2}, "a2"),
            ],
        ),
        (
            "result_for_wrong_attempt",
            lambda: [
                *invoiced(),
                ev(4, "payment.attempted", "c1", ATT, "a", "i"),
                ev(5, "payment.succeeded", "c1", {**RESULT, "attempt_number": 2}, "p", "a"),
            ],
        ),
        (
            "payment_after_paid",
            lambda: [
                *invoiced(),
                ev(4, "payment.attempted", "c1", ATT, "a", "i"),
                ev(5, "payment.succeeded", "c1", RESULT, "p", "a"),
                ev(6, "payment.attempted", "c1", {**ATT, "attempt_number": 2}, "a2"),
            ],
        ),
        (
            "invoice_for_churned_customer",
            lambda: [
                *active_prefix(),
                ev(3, "churn.observed", "c1", {"reason": "voluntary", "tenure_days": 6}, "x"),
                ev(4, "invoice.created", "c1", INV, "i"),
            ],
        ),
        (
            "event_before_customer_exists_via_churn",
            lambda: [ev(1, "churn.observed", "c1", {"reason": "voluntary", "tenure_days": 6}, "x")],
        ),
    ],
)
def test_validator_rejects_more_impossible_streams(name: str, events: Any) -> None:
    with pytest.raises(StreamViolation):
        feed_all(events())


def test_retry_after_failure_then_success_is_legal() -> None:
    failed = {**RESULT, "reason": "card_declined", "final": False}
    feed_all(
        [
            *invoiced(),
            ev(4, "payment.attempted", "c1", ATT, "a1", "i"),
            ev(5, "payment.failed", "c1", failed, "f1", "a1"),
            ev(6, "payment.attempted", "c1", {**ATT, "attempt_number": 2}, "a2", "f1"),
            ev(7, "payment.succeeded", "c1", {**RESULT, "attempt_number": 2}, "p2", "a2"),
        ]
    )


def test_final_failure_is_terminal_for_the_invoice() -> None:
    final = {**RESULT, "attempt_number": 3, "reason": "card_declined", "final": True}
    att3 = {**ATT, "attempt_number": 3}
    v = StreamValidator(3)
    for e in [
        *invoiced(),
        ev(4, "payment.attempted", "c1", ATT, "a1", "i"),
        ev(
            5,
            "payment.failed",
            "c1",
            {**RESULT, "reason": "card_declined", "final": False},
            "f1",
            "a1",
        ),
        ev(6, "payment.attempted", "c1", {**ATT, "attempt_number": 2}, "a2", "f1"),
        ev(
            7,
            "payment.failed",
            "c1",
            {**RESULT, "attempt_number": 2, "reason": "card_declined", "final": False},
            "f2",
            "a2",
        ),
        ev(8, "payment.attempted", "c1", att3, "a3", "f2"),
        ev(9, "payment.failed", "c1", final, "f3", "a3"),
    ]:
        v.feed(e)
    with pytest.raises(StreamViolation):
        v.feed(ev(10, "payment.attempted", "c1", {**ATT, "attempt_number": 4}, "a4"))
    assert v.finish()["payment.failed"] == 3
