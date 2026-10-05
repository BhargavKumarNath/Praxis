"""Order-independent aggregate projections for the operational control plane.

Delivery is at-least-once and may be delayed or reordered, so the control plane never
applies an event "in place" to whatever state it happens to find. Instead every accepted
lifecycle event is logged per aggregate, and the aggregate's state is the *fold* of that
log in canonical order. The fold is a pure function of the event **set**:

* duplicates cannot change it (events are keyed by ``event_id``),
* arrival order cannot change it (events are sorted by a canonical key first),
* an event whose predecessor has not arrived yet is *pending*, not an error; it is
  applied automatically by the fold that runs after the predecessor arrives.

Every applied event goes through a ``TransitionTable``; there is no other way to change a
state. Folding skips (and reports as pending) an event that is not valid at its position
and keeps going, so a single bogus event cannot block the rest of an aggregate.

Aggregates are independent (customer, invoice). Cross-aggregate rules such as "invoices
only for active customers" are checked analytically (dbt invariants), not here, because
the two aggregates' events may legitimately arrive in either order.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from praxis.domain.states import (
    CUSTOMER_TRANSITIONS,
    INVOICE_TRANSITIONS,
    SUBSCRIPTION_TRANSITIONS,
    CustomerState,
    InvalidTransition,
    InvoiceState,
    SubscriptionState,
)


class AggregateKind(StrEnum):
    CUSTOMER = "customer"
    INVOICE = "invoice"


class Machine(StrEnum):
    CUSTOMER = "customer"
    SUBSCRIPTION = "subscription"
    INVOICE = "invoice"


# Same-timestamp tie-break inside a customer aggregate (lifecycle order).
_CUSTOMER_RANK: dict[str, int] = {
    "customer.created": 0,
    "conversion.observed": 1,
    "subscription.started": 2,
    "subscription.changed": 3,
    "churn.observed": 4,
}
_INVOICE_PHASE: dict[str, int] = {
    "invoice.created": 0,
    "payment.attempted": 1,
    "payment.failed": 2,
    "payment.succeeded": 2,
}

CUSTOMER_EVENT_TYPES = frozenset(_CUSTOMER_RANK)
INVOICE_EVENT_TYPES = frozenset(_INVOICE_PHASE)
# Event types that mutate control-plane state. Everything else (usage, requests, prices,
# service metrics) is analytical and never reaches Postgres (ADR 0002).
STATEFUL_EVENT_TYPES = CUSTOMER_EVENT_TYPES | INVOICE_EVENT_TYPES


def aggregate_of(
    event_type: str, entity_id: str | None, payload: Mapping[str, Any]
) -> tuple[AggregateKind, str] | None:
    """The aggregate an event belongs to, or ``None`` for non-stateful events."""
    if event_type in CUSTOMER_EVENT_TYPES and entity_id:
        return AggregateKind.CUSTOMER, entity_id
    if event_type in INVOICE_EVENT_TYPES:
        invoice_id = payload.get("invoice_id")
        if isinstance(invoice_id, str) and invoice_id:
            return AggregateKind.INVOICE, invoice_id
    return None


@dataclass(frozen=True, slots=True)
class LoggedEvent:
    event_id: str
    event_type: str
    entity_id: str
    occurred_at: datetime
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Transition:
    machine: Machine
    from_state: str | None
    to_state: str
    event_id: str
    occurred_at: datetime


@dataclass(frozen=True)
class CustomerProjection:
    customer_id: str
    state: CustomerState | None = None
    region_id: str | None = None
    industry: str | None = None
    tier: str | None = None
    is_existing: bool | None = None
    subscription_state: SubscriptionState | None = None
    products: tuple[str, ...] = ()
    base_fee_minor: int | None = None
    billing_period_days: int | None = None
    churn_reason: str | None = None
    last_applied_at: datetime | None = None
    applied: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    transitions: tuple[Transition, ...] = ()


@dataclass(frozen=True)
class InvoiceProjection:
    invoice_id: str
    state: InvoiceState | None = None
    customer_id: str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    attempts: int = 0
    amount_paid_minor: int = 0
    paid_event_id: str | None = None
    paid_at: datetime | None = None
    last_failure_reason: str | None = None
    last_applied_at: datetime | None = None
    applied: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    transitions: tuple[Transition, ...] = field(default=())


def _customer_key(e: LoggedEvent) -> tuple[datetime, int, str]:
    return e.occurred_at, _CUSTOMER_RANK.get(e.event_type, 99), e.event_id


def _invoice_key(e: LoggedEvent) -> tuple[int, int, str]:
    attempt = e.payload.get("attempt_number", 0)
    return (
        int(attempt) if isinstance(attempt, int) else 0,
        _INVOICE_PHASE.get(e.event_type, 99),
        e.event_id,
    )


def _unique(events: Iterable[LoggedEvent]) -> list[LoggedEvent]:
    return list({e.event_id: e for e in events}.values())


def fold_customer(customer_id: str, events: Iterable[LoggedEvent]) -> CustomerProjection:
    proj = CustomerProjection(customer_id)
    applied: list[str] = []
    pending: list[str] = []
    transitions: list[Transition] = []
    for e in sorted(_unique(events), key=_customer_key):
        try:
            proj, new = _apply_customer(proj, e)
        except InvalidTransition:
            pending.append(e.event_id)
            continue
        applied.append(e.event_id)
        transitions.extend(new)
    return replace(
        proj, applied=tuple(applied), pending=tuple(pending), transitions=tuple(transitions)
    )


def _apply_customer(  # noqa: C901 - complexity-debt
    p: CustomerProjection, e: LoggedEvent
) -> tuple[CustomerProjection, list[Transition]]:
    """Validate everything first, then return the new projection. Raises InvalidTransition."""
    et, pl = e.event_type, e.payload
    if e.entity_id != p.customer_id:
        raise InvalidTransition(p.state, f"{et}[foreign entity]")

    def tr(machine: Machine, src: StrEnum | None, dst: StrEnum) -> Transition:
        return Transition(
            machine, None if src is None else src.value, dst.value, e.event_id, e.occurred_at
        )

    if et == "customer.created":
        if p.state is not None:
            raise InvalidTransition(p.state, et)
        state = CUSTOMER_TRANSITIONS.start(et)
        return replace(
            p,
            state=state,
            region_id=str(pl["region_id"]),
            industry=str(pl["industry"]),
            tier=str(pl["tier"]),
            is_existing=bool(pl["is_existing"]),
            last_applied_at=e.occurred_at,
        ), [tr(Machine.CUSTOMER, None, state)]
    if p.state is None:
        raise InvalidTransition(None, f"{et}[before customer.created]")
    if et == "conversion.observed":
        trigger = "conversion.converted" if pl["converted"] else "conversion.not_converted"
        state = CUSTOMER_TRANSITIONS.apply(p.state, trigger)
        return replace(p, state=state, last_applied_at=e.occurred_at), [
            tr(Machine.CUSTOMER, p.state, state)
        ]
    if et == "subscription.started":
        state = CUSTOMER_TRANSITIONS.apply(p.state, et)
        if (p.state is CustomerState.CONVERTED) != (pl["origin"] == "new"):
            raise InvalidTransition(p.state, f"{et}[origin={pl['origin']}]")
        if pl["tier"] != p.tier or p.subscription_state is not None:
            raise InvalidTransition(p.state, f"{et}[tier or duplicate subscription]")
        sub = SUBSCRIPTION_TRANSITIONS.start(et)
        return replace(
            p,
            state=state,
            subscription_state=sub,
            products=tuple(str(x) for x in pl["products"]),
            base_fee_minor=int(pl["base_fee_minor"]),
            billing_period_days=int(pl["billing_period_days"]),
            last_applied_at=e.occurred_at,
        ), [tr(Machine.CUSTOMER, p.state, state), tr(Machine.SUBSCRIPTION, None, sub)]
    if et == "subscription.changed":
        state = CUSTOMER_TRANSITIONS.apply(p.state, et)
        sub = SUBSCRIPTION_TRANSITIONS.apply(_need_sub(p, et), et)
        if pl["from_tier"] != p.tier or pl["from_tier"] == pl["to_tier"]:
            raise InvalidTransition(p.state, f"{et}[tier mismatch]")
        return replace(
            p,
            state=state,
            subscription_state=sub,
            tier=str(pl["to_tier"]),
            base_fee_minor=int(pl["base_fee_minor"]),
            last_applied_at=e.occurred_at,
        ), [
            tr(Machine.CUSTOMER, p.state, state),
            tr(Machine.SUBSCRIPTION, p.subscription_state, sub),
        ]
    if et == "churn.observed":
        state = CUSTOMER_TRANSITIONS.apply(p.state, et)
        sub = SUBSCRIPTION_TRANSITIONS.apply(_need_sub(p, et), et)
        return replace(
            p,
            state=state,
            subscription_state=sub,
            churn_reason=str(pl["reason"]),
            last_applied_at=e.occurred_at,
        ), [
            tr(Machine.CUSTOMER, p.state, state),
            tr(Machine.SUBSCRIPTION, p.subscription_state, sub),
        ]
    raise InvalidTransition(p.state, et)


def _need_sub(p: CustomerProjection, trigger: str) -> SubscriptionState:
    if p.subscription_state is None:
        raise InvalidTransition(None, f"{trigger}[no subscription]")
    return p.subscription_state


def fold_invoice(invoice_id: str, events: Iterable[LoggedEvent]) -> InvoiceProjection:
    proj = InvoiceProjection(invoice_id)
    applied: list[str] = []
    pending: list[str] = []
    transitions: list[Transition] = []
    for e in sorted(_unique(events), key=_invoice_key):
        try:
            proj, new = _apply_invoice(proj, e)
        except InvalidTransition:
            pending.append(e.event_id)
            continue
        applied.append(e.event_id)
        transitions.append(new)
    return replace(
        proj, applied=tuple(applied), pending=tuple(pending), transitions=tuple(transitions)
    )


def _apply_invoice(p: InvoiceProjection, e: LoggedEvent) -> tuple[InvoiceProjection, Transition]:  # noqa: C901 - complexity-debt
    et, pl = e.event_type, e.payload
    if pl.get("invoice_id") != p.invoice_id:
        raise InvalidTransition(p.state, f"{et}[foreign invoice]")

    def tr(src: InvoiceState | None, dst: InvoiceState) -> Transition:
        return Transition(
            Machine.INVOICE,
            None if src is None else src.value,
            dst.value,
            e.event_id,
            e.occurred_at,
        )

    if et == "invoice.created":
        if p.state is not None:
            raise InvalidTransition(p.state, et)
        state = INVOICE_TRANSITIONS.start(et)
        return replace(
            p,
            state=state,
            customer_id=e.entity_id,
            amount_minor=int(pl["amount_minor"]),
            currency=str(pl["currency"]),
            period_start=str(pl["period_start"]),
            period_end=str(pl["period_end"]),
            last_applied_at=e.occurred_at,
        ), tr(None, state)
    if p.state is None:
        raise InvalidTransition(None, f"{et}[before invoice.created]")
    if e.entity_id != p.customer_id or pl["amount_minor"] != p.amount_minor:
        raise InvalidTransition(p.state, f"{et}[customer or amount mismatch]")
    if pl["currency"] != p.currency:
        raise InvalidTransition(p.state, f"{et}[currency mismatch]")
    number = pl["attempt_number"]
    if et == "payment.attempted":
        if number != p.attempts + 1:
            raise InvalidTransition(p.state, f"{et}[attempt {number} after {p.attempts}]")
        state = INVOICE_TRANSITIONS.apply(p.state, et)
        return replace(p, state=state, attempts=number, last_applied_at=e.occurred_at), tr(
            p.state, state
        )
    if number != p.attempts:
        raise InvalidTransition(
            p.state, f"{et}[result for attempt {number}, in flight {p.attempts}]"
        )
    if et == "payment.succeeded":
        state = INVOICE_TRANSITIONS.apply(p.state, et)
        assert p.amount_minor is not None  # noqa: S101 - set by invoice.created
        return replace(
            p,
            state=state,
            amount_paid_minor=p.amount_minor,
            paid_event_id=e.event_id,
            paid_at=e.occurred_at,
            last_applied_at=e.occurred_at,
        ), tr(p.state, state)
    if et == "payment.failed":
        trigger = "payment.failed.final" if pl["final"] else "payment.failed.retry"
        state = INVOICE_TRANSITIONS.apply(p.state, trigger)
        return replace(
            p, state=state, last_failure_reason=str(pl["reason"]), last_applied_at=e.occurred_at
        ), tr(p.state, state)
    raise InvalidTransition(p.state, et)
