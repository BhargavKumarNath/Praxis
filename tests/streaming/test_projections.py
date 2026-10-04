"""Order-independent projections: unit branches and invariant (property) tests."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from praxis.domain.projections import (
    STATEFUL_EVENT_TYPES,
    AggregateKind,
    Machine,
    aggregate_of,
    fold_customer,
    fold_invoice,
)
from praxis.domain.states import (
    CUSTOMER_TRANSITIONS,
    INVOICE_TRANSITIONS,
    SUBSCRIPTION_TRANSITIONS,
    CustomerState,
    InvoiceState,
    SubscriptionState,
)
from praxis.simulator.validation import StreamValidator
from tests.streaming.helpers import (
    customer_lifecycle,
    ev,
    invoice_lifecycle,
    logged,
    sim_events,
)


def _fold_c(events: list[dict[str, Any]], cid: str = "cust_a") -> Any:
    return fold_customer(cid, [logged(e) for e in events])


def _fold_i(events: list[dict[str, Any]], inv: str = "inv_1") -> Any:
    return fold_invoice(inv, [logged(e) for e in events])


# --- customer aggregate --------------------------------------------------------------
def test_full_new_customer_lifecycle() -> None:
    p = _fold_c(customer_lifecycle(changes=2))
    assert p.state is CustomerState.CHURNED
    assert p.subscription_state is SubscriptionState.CANCELLED
    assert p.tier == "enterprise"
    assert p.base_fee_minor == 9_901
    assert p.churn_reason == "voluntary"
    assert p.pending == ()
    assert len(p.applied) == 6
    machines = [(t.machine, t.from_state, t.to_state) for t in p.transitions]
    assert machines[0] == (Machine.CUSTOMER, None, "prospect")
    assert (Machine.SUBSCRIPTION, "active", "cancelled") in machines


def test_existing_customer_skips_conversion() -> None:
    p = _fold_c(customer_lifecycle(new=False, changes=0, churn=False))
    assert p.state is CustomerState.ACTIVE
    assert p.is_existing is True
    assert p.products == ("gpu-inference",)


def test_not_converted_is_terminal_lost() -> None:
    created = customer_lifecycle(changes=0, churn=False)[0]
    lost = ev(
        "conv:x",
        "conversion.observed",
        "cust_a",
        {"converted": False, "price_index_milli": 900},
        180,
    )
    p = _fold_c([created, lost])
    assert p.state is CustomerState.LOST
    assert p.subscription_state is None


@pytest.mark.parametrize(
    ("drop", "pending_types"),
    [
        (
            "customer.created",
            {
                "conversion.observed",
                "subscription.started",
                "subscription.changed",
                "churn.observed",
            },
        ),
        ("conversion.observed", {"subscription.started", "subscription.changed", "churn.observed"}),
        ("subscription.started", {"subscription.changed", "churn.observed"}),
    ],
)
def test_missing_predecessor_leaves_successors_pending(drop: str, pending_types: set[str]) -> None:
    events = [e for e in customer_lifecycle() if e["event_type"] != drop]
    p = _fold_c(events)
    by_id = {e["event_id"]: e["event_type"] for e in events}
    assert {by_id[i] for i in p.pending} == pending_types


def test_gap_in_tier_changes_is_detected() -> None:
    events = customer_lifecycle(changes=2, churn=False)
    first_change = next(e for e in events if e["event_type"] == "subscription.changed")
    p = _fold_c([e for e in events if e is not first_change])
    assert p.tier == "starter"  # second change (growth -> enterprise) cannot apply yet
    assert len(p.pending) == 1


def test_churn_before_missing_tier_change_converges_when_it_arrives() -> None:
    events = customer_lifecycle(changes=1)
    change = next(e for e in events if e["event_type"] == "subscription.changed")
    partial = _fold_c([e for e in events if e is not change])
    assert partial.state is CustomerState.CHURNED and partial.tier == "starter"
    full = _fold_c(events)
    assert full.state is CustomerState.CHURNED and full.tier == "growth"


@pytest.mark.parametrize(
    "bad",
    [
        ev(
            "dup-created",
            "customer.created",
            "cust_a",
            {
                "region_id": "r",
                "industry": "i",
                "tier": "starter",
                "preferred_payment_method": "card",
                "is_existing": False,
                "tenure_days": 0,
            },
            61,
        ),
        ev(
            "foreign",
            "churn.observed",
            "cust_zzz",
            {"reason": "voluntary", "tenure_days": 1},
            9 * 86_400,
        ),
        ev(
            "same-tier",
            "subscription.changed",
            "cust_a",
            {"from_tier": "growth", "to_tier": "growth", "base_fee_minor": 1},
            5 * 86_400,
        ),
        ev(
            "wrong-origin",
            "subscription.started",
            "cust_a",
            {
                "tier": "starter",
                "products": ["x"],
                "origin": "existing",
                "base_fee_minor": 1,
                "billing_period_days": 30,
            },
            250,
        ),
        ev(
            "unrelated",
            "usage.observed",
            "cust_a",
            {
                "product": "x",
                "region_id": "r",
                "units": 1,
                "throttled_units": 0,
                "unit_price_micros": 1,
            },
            300,
        ),
    ],
)
def test_invalid_event_is_pending_and_does_not_block_others(bad: dict[str, Any]) -> None:
    good = _fold_c(customer_lifecycle())
    p = _fold_c([*customer_lifecycle(), bad])
    assert p.pending == (bad["event_id"],)
    assert replace(p, pending=(), transitions=()) == replace(good, transitions=())


def test_change_and_churn_require_a_subscription() -> None:
    created = customer_lifecycle(new=False, changes=0, churn=False)[0]
    churn = customer_lifecycle(new=False, changes=0)[-1]
    p = _fold_c([created, churn])
    assert p.state is CustomerState.PROSPECT
    assert p.pending == (churn["event_id"],)


# --- invoice aggregate ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("fail_first", "state", "attempts", "paid"),
    [
        (0, InvoiceState.PAID, 1, 12_345),
        (2, InvoiceState.PAID, 3, 12_345),
        (3, InvoiceState.UNCOLLECTIBLE, 3, 0),
    ],
)
def test_invoice_outcomes(fail_first: int, state: InvoiceState, attempts: int, paid: int) -> None:
    p = _fold_i(invoice_lifecycle(fail_first=fail_first))
    assert (p.state, p.attempts, p.amount_paid_minor, p.pending) == (state, attempts, paid, ())
    if fail_first:
        assert p.last_failure_reason == "card_declined"


def test_payment_before_invoice_is_pending() -> None:
    events = invoice_lifecycle()
    p = _fold_i(events[1:])
    assert p.state is None and len(p.pending) == 2


@pytest.mark.parametrize(
    ("patch", "where"),
    [
        ({"amount_minor": 1}, "payment.attempted"),
        ({"currency": "EUR"}, "payment.attempted"),
        ({"attempt_number": 2}, "payment.attempted"),
        ({"attempt_number": 2}, "payment.succeeded"),
        ({"invoice_id": "inv_other"}, "payment.attempted"),
    ],
)
def test_inconsistent_payment_events_stay_pending(patch: dict[str, Any], where: str) -> None:
    events = invoice_lifecycle()
    target = next(e for e in events if e["event_type"] == where)
    target["payload"] = {**target["payload"], **patch}
    p = _fold_i(events)
    assert target["event_id"] in p.pending
    assert p.state is not InvoiceState.PAID


def test_payment_from_another_customer_is_rejected() -> None:
    events = invoice_lifecycle()
    events[1]["entity_id"] = "cust_other"
    assert events[1]["event_id"] in _fold_i(events).pending


def test_duplicate_success_with_new_id_cannot_pay_twice() -> None:
    events = invoice_lifecycle()
    twin = dict(events[-1], event_id="00000000-0000-4000-8000-000000000001")
    p = _fold_i([*events, twin])
    assert p.state is InvoiceState.PAID and p.amount_paid_minor == 12_345
    assert len(p.pending) == 1


def test_second_invoice_created_is_rejected() -> None:
    events = invoice_lifecycle()
    twin = dict(events[0], event_id="00000000-0000-4000-8000-000000000002")
    p = _fold_i([*events, twin])
    # Exactly one creation applies (deterministic tie-break on event_id); state unaffected.
    assert len(p.pending) == 1 and p.pending[0] in {twin["event_id"], events[0]["event_id"]}
    assert (p.state, p.amount_paid_minor) == (InvoiceState.PAID, 12_345)


def test_aggregate_routing() -> None:
    assert aggregate_of("churn.observed", "c1", {}) == (AggregateKind.CUSTOMER, "c1")
    assert aggregate_of("payment.failed", "c1", {"invoice_id": "i1"}) == (
        AggregateKind.INVOICE,
        "i1",
    )
    assert aggregate_of("payment.failed", "c1", {}) is None
    assert aggregate_of("usage.observed", "c1", {}) is None
    assert aggregate_of("customer.created", None, {}) is None
    assert {"usage.observed", "price.exposed", "service.metric_observed"}.isdisjoint(
        STATEFUL_EVENT_TYPES
    )


# --- invariants ----------------------------------------------------------------------
LIFECYCLE = customer_lifecycle(changes=3) + invoice_lifecycle(fail_first=1)
_REACHABLE = {
    Machine.CUSTOMER: {(a.value, b.value) for a, b in CUSTOMER_TRANSITIONS.reachable_pairs()},
    Machine.SUBSCRIPTION: {
        (a.value, b.value) for a, b in SUBSCRIPTION_TRANSITIONS.reachable_pairs()
    },
    Machine.INVOICE: {(a.value, b.value) for a, b in INVOICE_TRANSITIONS.reachable_pairs()},
}


def _fold_both(events: list[dict[str, Any]]) -> tuple[Any, Any]:
    return (
        _fold_c(
            [
                e
                for e in events
                if e["event_type"] not in ("invoice.created",)
                and not e["event_type"].startswith("payment.")
            ]
        ),
        _fold_i(
            [
                e
                for e in events
                if e["event_type"] == "invoice.created" or e["event_type"].startswith("payment.")
            ]
        ),
    )


@settings(max_examples=300, deadline=None)
@given(st.permutations(LIFECYCLE), st.lists(st.integers(0, len(LIFECYCLE) - 1), max_size=10))
def test_fold_is_independent_of_order_and_duplicates(
    order: list[dict[str, Any]], dup_idx: list[int]
) -> None:
    delivered = [*order, *(order[i] for i in dup_idx)]
    assert _fold_both(delivered) == _fold_both(LIFECYCLE)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.booleans(), min_size=len(LIFECYCLE), max_size=len(LIFECYCLE)))
def test_any_subset_folds_monotonically_towards_the_full_state(keep: list[bool]) -> None:
    """Adding the missing events to any partial log only moves states forward."""
    subset = [e for e, k in zip(LIFECYCLE, keep, strict=True) if k]
    part_c, part_i = _fold_both(subset)
    full_c, full_i = _fold_both(LIFECYCLE)
    pairs = [
        (Machine.CUSTOMER, part_c.state, full_c.state),
        (Machine.SUBSCRIPTION, part_c.subscription_state, full_c.subscription_state),
        (Machine.INVOICE, part_i.state, full_i.state),
    ]
    for machine, part, full in pairs:
        if part is not None:
            assert (part.value, full.value) in _REACHABLE[machine]
    for proj in (part_c, part_i):
        for t in proj.transitions:
            if t.from_state is not None:
                assert (t.from_state, t.to_state) in _REACHABLE[t.machine]


def test_fold_matches_phase1_validator_on_simulated_stream() -> None:
    events = sim_events(200, 56)
    validator = StreamValidator(3, schema_every=1_000_000)
    customers: dict[str, list[Any]] = defaultdict(list)
    invoices: dict[str, list[Any]] = defaultdict(list)
    for e in events:
        validator.feed(e)
        agg = aggregate_of(e["event_type"], e["entity_id"], e["payload"])
        if agg is not None:
            (customers if agg[0] is AggregateKind.CUSTOMER else invoices)[agg[1]].append(logged(e))
    expected_c = validator.customer_states()
    expected_i = validator.invoice_states()
    assert set(customers) == set(expected_c) and set(invoices) == set(expected_i)
    for cid, evs in customers.items():
        p = fold_customer(cid, list(reversed(evs)))
        assert (p.state, p.tier, p.pending) == (*expected_c[cid], ())
    for inv, evs in invoices.items():
        q = fold_invoice(inv, list(reversed(evs)))
        cust, amount, state, attempts = expected_i[inv]
        assert (q.customer_id, q.amount_minor, q.state, q.attempts, q.pending) == (
            cust,
            amount,
            state,
            attempts,
            (),
        )
    assert {s for s, _ in expected_c.values()} >= {CustomerState.ACTIVE, CustomerState.LOST}
    assert {s for *_, s, _ in expected_i.values()} >= {
        InvoiceState.PAID,
        InvoiceState.UNCOLLECTIBLE,
    }
