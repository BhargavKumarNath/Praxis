"""Explicit lifecycle state machines (customer and invoice).

State changes are only possible through ``TransitionTable.apply``; arbitrary updates are
not representable. Tables are data, so tests can enumerate every allowed and forbidden
(state, trigger) pair.

Customer lifecycle
    PROSPECT  --customer.created-->            (entry)
    PROSPECT  --conversion.converted-->        CONVERTED
    PROSPECT  --conversion.not_converted-->    LOST       (terminal)
    CONVERTED --subscription.started-->        ACTIVE
    PROSPECT  --subscription.started-->        ACTIVE     (pre-existing customers only)
    ACTIVE    --subscription.changed-->        ACTIVE
    ACTIVE    --churn.observed-->              CHURNED    (terminal)

Invoice lifecycle (attempt bookkeeping lives in the stream validator)
    (none) --invoice.created--> OPEN
    OPEN --payment.attempted--> ATTEMPTING
    ATTEMPTING --payment.failed (retry)--> OPEN
    ATTEMPTING --payment.failed (final)--> UNCOLLECTIBLE (terminal)
    ATTEMPTING --payment.succeeded--> PAID (terminal)

Subscription lifecycle (Phase 3, one subscription per customer)
    (none)  --subscription.started-->  ACTIVE
    ACTIVE  --subscription.changed-->  ACTIVE     (tier change)
    ACTIVE  --churn.observed-->        CANCELLED  (terminal)

The exact dunning states for Phase 8 are deliberately left to a later ADR; the invoice
machine here only models what the simulator emits.

``reachable_pairs`` is the reflexive-transitive closure of a table. The control plane
(Phase 3) installs it as a database trigger so a stored state can only ever move along
the machine, even when several buffered events are applied in one transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class InvalidTransition(ValueError):
    def __init__(self, state: object, trigger: str) -> None:
        super().__init__(f"forbidden transition from {state!r} on {trigger!r}")
        self.state = state
        self.trigger = trigger


class CustomerState(StrEnum):
    PROSPECT = "prospect"
    CONVERTED = "converted"
    ACTIVE = "active"
    CHURNED = "churned"
    LOST = "lost"


class InvoiceState(StrEnum):
    OPEN = "open"
    ATTEMPTING = "attempting"
    PAID = "paid"
    UNCOLLECTIBLE = "uncollectible"


class SubscriptionState(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"


class TransitionTable[S: StrEnum]:
    def __init__(self, table: Mapping[tuple[S, str], S], entry: Mapping[str, S]) -> None:
        self._table = dict(table)
        self._entry = dict(entry)

    @property
    def triggers(self) -> frozenset[str]:
        return frozenset({t for _, t in self._table} | set(self._entry))

    def start(self, trigger: str) -> S:
        try:
            return self._entry[trigger]
        except KeyError:
            raise InvalidTransition(None, trigger) from None

    def apply(self, state: S, trigger: str) -> S:
        try:
            return self._table[(state, trigger)]
        except KeyError:
            raise InvalidTransition(state, trigger) from None

    def allowed(self, state: S) -> frozenset[str]:
        return frozenset(t for (s, t) in self._table if s == state)

    def entries(self) -> frozenset[str]:
        return frozenset(self._entry)

    def reachable_pairs(self) -> frozenset[tuple[S, S]]:
        """Every (from, to) such that ``to`` is reachable from ``from`` in zero or more steps."""
        states = {s for s, _ in self._table} | set(self._table.values()) | set(self._entry.values())
        edges: dict[S, set[S]] = {s: set() for s in states}
        for (src, _), dst in self._table.items():
            edges[src].add(dst)
        pairs: set[tuple[S, S]] = set()
        for start in states:
            seen = {start}
            frontier = [start]
            while frontier:
                for nxt in edges[frontier.pop()]:
                    if nxt not in seen:
                        seen.add(nxt)
                        frontier.append(nxt)
            pairs.update((start, s) for s in seen)
        return frozenset(pairs)


CUSTOMER_TRANSITIONS: TransitionTable[CustomerState] = TransitionTable(
    {
        (CustomerState.PROSPECT, "conversion.converted"): CustomerState.CONVERTED,
        (CustomerState.PROSPECT, "conversion.not_converted"): CustomerState.LOST,
        (CustomerState.CONVERTED, "subscription.started"): CustomerState.ACTIVE,
        (CustomerState.PROSPECT, "subscription.started"): CustomerState.ACTIVE,
        (CustomerState.ACTIVE, "subscription.changed"): CustomerState.ACTIVE,
        (CustomerState.ACTIVE, "churn.observed"): CustomerState.CHURNED,
    },
    entry={"customer.created": CustomerState.PROSPECT},
)

INVOICE_TRANSITIONS: TransitionTable[InvoiceState] = TransitionTable(
    {
        (InvoiceState.OPEN, "payment.attempted"): InvoiceState.ATTEMPTING,
        (InvoiceState.ATTEMPTING, "payment.failed.retry"): InvoiceState.OPEN,
        (InvoiceState.ATTEMPTING, "payment.failed.final"): InvoiceState.UNCOLLECTIBLE,
        (InvoiceState.ATTEMPTING, "payment.succeeded"): InvoiceState.PAID,
    },
    entry={"invoice.created": InvoiceState.OPEN},
)

SUBSCRIPTION_TRANSITIONS: TransitionTable[SubscriptionState] = TransitionTable(
    {
        (SubscriptionState.ACTIVE, "subscription.changed"): SubscriptionState.ACTIVE,
        (SubscriptionState.ACTIVE, "churn.observed"): SubscriptionState.CANCELLED,
    },
    entry={"subscription.started": SubscriptionState.ACTIVE},
)

# Events a customer may emit only while in one of these states. Invoice/payment events are
# governed by the invoice machine: settlement may complete after a customer has churned.
CUSTOMER_STATES_ALLOWING: dict[str, frozenset[CustomerState]] = {
    "price.exposed": frozenset({CustomerState.PROSPECT, CustomerState.ACTIVE}),
    "usage.observed": frozenset({CustomerState.ACTIVE}),
    "request.completed": frozenset({CustomerState.ACTIVE}),
    "invoice.created": frozenset({CustomerState.ACTIVE}),
    "payment.attempted": frozenset({CustomerState.ACTIVE, CustomerState.CHURNED}),
    "payment.failed": frozenset({CustomerState.ACTIVE, CustomerState.CHURNED}),
    "payment.succeeded": frozenset({CustomerState.ACTIVE, CustomerState.CHURNED}),
}
