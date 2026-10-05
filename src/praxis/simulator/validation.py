"""Stream validator: enforces schemas, state machines and temporal invariants.

Feed events in stream order. Any impossible state transition, backwards entity
timestamp, duplicate ``event_id``, dangling causation reference or malformed payload
raises ``StreamViolation``. Pydantic schema validation can be sampled
(``schema_every``) for very large streams; structural checks always run on every event.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from praxis.domain.states import (
    CUSTOMER_STATES_ALLOWING,
    CUSTOMER_TRANSITIONS,
    INVOICE_TRANSITIONS,
    CustomerState,
    InvalidTransition,
    InvoiceState,
)
from praxis.events.envelope import EventEnvelope
from praxis.events.payloads import PAYLOAD_MODELS, validate_payload


class StreamViolation(AssertionError):
    pass


@dataclass
class _Invoice:
    customer: str
    amount: int
    state: InvoiceState
    attempts: int = 0


@dataclass
class StreamValidator:
    max_attempts: int
    schema_every: int = 1
    counts: Counter[str] = field(default_factory=Counter)
    _seen_ids: set[str] = field(default_factory=set)
    _entity_last: dict[str, str] = field(default_factory=dict)
    _customer: dict[str, CustomerState] = field(default_factory=dict)
    _tier: dict[str, str] = field(default_factory=dict)
    _invoices: dict[str, _Invoice] = field(default_factory=dict)
    _last_ts: str = ""
    _n: int = 0

    def feed(self, e: dict[str, Any]) -> None:
        self._n += 1
        etype: str = e["event_type"]
        if etype not in PAYLOAD_MODELS:
            raise StreamViolation(f"unknown event type {etype}")
        if self._n % self.schema_every == 0:
            try:
                EventEnvelope.model_validate(e)
                validate_payload(etype, e["payload"])
            except ValidationError as exc:
                raise StreamViolation(f"schema violation in {etype}: {exc}") from exc
        self._check_envelope(e)
        self.counts[etype] += 1
        p = e["payload"]
        entity: str = e["entity_id"]
        if etype == "service.metric_observed":
            return
        self._check_customer(etype, entity, p)
        if etype.startswith(("invoice.", "payment.")):
            self._check_invoice(etype, entity, p)

    def _check_envelope(self, e: dict[str, Any]) -> None:
        eid, when, entity = e["event_id"], e["occurred_at"], e["entity_id"]
        if eid in self._seen_ids:
            raise StreamViolation(f"duplicate event_id {eid}")
        cause = e["causation_id"]
        if cause is not None and cause not in self._seen_ids:
            raise StreamViolation(f"dangling causation_id on {e['event_type']}")
        self._seen_ids.add(eid)
        if when < self._last_ts:
            raise StreamViolation("global stream time moved backwards")
        self._last_ts = when
        if when < self._entity_last.get(entity, ""):
            raise StreamViolation(f"timestamp moved backwards for entity {entity}")
        self._entity_last[entity] = when

    def _check_customer(self, etype: str, cid: str, p: dict[str, Any]) -> None:  # noqa: C901 - complexity-debt
        state = self._customer.get(cid)
        try:
            if etype == "customer.created":
                if state is not None:
                    raise InvalidTransition(state, etype)
                self._customer[cid] = CUSTOMER_TRANSITIONS.start(etype)
                self._tier[cid] = p["tier"]
            elif etype == "conversion.observed":
                trig = "conversion.converted" if p["converted"] else "conversion.not_converted"
                self._customer[cid] = CUSTOMER_TRANSITIONS.apply(_need(state), trig)
            elif etype == "subscription.started":
                new_state = CUSTOMER_TRANSITIONS.apply(_need(state), etype)
                if (state is CustomerState.CONVERTED) != (p["origin"] == "new"):
                    raise InvalidTransition(state, f"{etype}[origin={p['origin']}]")
                self._customer[cid] = new_state
                if p["tier"] != self._tier[cid]:
                    raise StreamViolation("subscription tier differs from customer tier")
            elif etype == "subscription.changed":
                self._customer[cid] = CUSTOMER_TRANSITIONS.apply(_need(state), etype)
                if p["from_tier"] != self._tier[cid] or p["from_tier"] == p["to_tier"]:
                    raise StreamViolation("subscription.changed tier mismatch")
                self._tier[cid] = p["to_tier"]
            elif etype == "churn.observed":
                self._customer[cid] = CUSTOMER_TRANSITIONS.apply(_need(state), etype)
            elif etype in CUSTOMER_STATES_ALLOWING:
                if state not in CUSTOMER_STATES_ALLOWING[etype]:
                    raise InvalidTransition(state, etype)
        except InvalidTransition as exc:
            raise StreamViolation(f"{exc} (customer {cid})") from exc

    def _check_invoice(self, etype: str, cid: str, p: dict[str, Any]) -> None:  # noqa: C901 - complexity-debt
        inv_id = p["invoice_id"]
        try:
            if etype == "invoice.created":
                if inv_id in self._invoices:
                    raise InvalidTransition(None, "duplicate invoice")
                self._invoices[inv_id] = _Invoice(
                    cid, p["amount_minor"], INVOICE_TRANSITIONS.start(etype)
                )
                return
            inv = self._invoices.get(inv_id)
            if inv is None or inv.customer != cid:
                raise StreamViolation(f"{etype} for unknown invoice or wrong customer")
            if p["amount_minor"] != inv.amount:
                raise StreamViolation("payment amount differs from invoice amount")
            if etype == "payment.attempted":
                if p["attempt_number"] != inv.attempts + 1:
                    raise StreamViolation("attempt numbers must increase by one")
                inv.attempts += 1
                inv.state = INVOICE_TRANSITIONS.apply(inv.state, etype)
                return
            if p["attempt_number"] != inv.attempts:
                raise StreamViolation("result does not match the in-flight attempt")
            if etype == "payment.succeeded":
                inv.state = INVOICE_TRANSITIONS.apply(inv.state, etype)
            else:
                if p["final"] != (p["attempt_number"] >= self.max_attempts):
                    raise StreamViolation("final flag inconsistent with attempt limit")
                trigger = "payment.failed.final" if p["final"] else "payment.failed.retry"
                inv.state = INVOICE_TRANSITIONS.apply(inv.state, trigger)
        except InvalidTransition as exc:
            raise StreamViolation(f"{exc} (invoice {inv_id})") from exc

    def finish(self) -> Counter[str]:
        return self.counts

    def customer_states(self) -> dict[str, tuple[CustomerState, str]]:
        """Final (state, tier) per customer after the events fed so far (read-only copy)."""
        return {cid: (state, self._tier[cid]) for cid, state in self._customer.items()}

    def invoice_states(self) -> dict[str, tuple[str, int, InvoiceState, int]]:
        """Final (customer, amount_minor, state, attempts) per invoice (read-only copy)."""
        return {k: (v.customer, v.amount, v.state, v.attempts) for k, v in self._invoices.items()}


def _need(state: CustomerState | None) -> CustomerState:
    if state is None:
        raise InvalidTransition(None, "event before customer.created")
    return state
