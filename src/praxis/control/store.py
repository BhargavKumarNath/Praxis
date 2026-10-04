"""Control-plane repository: idempotent, order-independent event application.

``apply_event`` runs one transaction per event:

1. insert ``(consumer, event_id)`` into ``processed_events``; if it already exists the
   event was fully applied before (duplicate delivery, redelivery after a crash between
   commit and ack, replay) and nothing else happens;
2. take a transaction-scoped advisory lock on the aggregate (serialises concurrent
   consumers of the same customer / invoice);
3. append the event to ``entity_events``;
4. refold the aggregate from its full event log (``praxis.domain.projections``) and
   upsert the projection, transitions and ledger.

Steps 1-4 commit together or not at all, so a crash at any point either leaves no trace
(redelivery reapplies) or leaves the event fully applied (redelivery is a no-op).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Connection, Engine, text

from praxis.control.db import transient_errors
from praxis.domain.projections import (
    AggregateKind,
    CustomerProjection,
    InvoiceProjection,
    LoggedEvent,
    Transition,
    aggregate_of,
    fold_customer,
    fold_invoice,
)
from praxis.domain.states import InvoiceState


class ApplyStatus(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_id: str
    event_type: str
    entity_id: str | None
    occurred_at: datetime
    payload: Mapping[str, Any]
    trace_id: str
    correlation_id: str
    causation_id: str | None
    delivery_attempt: int = 1


@dataclass(frozen=True, slots=True)
class ApplyResult:
    status: ApplyStatus
    aggregate: tuple[AggregateKind, str] | None = None
    pending: int = 0


@dataclass(frozen=True, slots=True)
class DeadLetterRecord:
    source_subscription: str
    reason: str
    detail: str | None
    event_id: str | None
    event_type: str | None
    trace_id: str | None
    correlation_id: str | None
    delivery_attempt: int | None
    data: bytes

    @property
    def data_sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def key(self) -> str:
        """Same original message dead-lettered from the same subscription = same key."""
        ident = self.event_id or self.data_sha256
        return hashlib.sha256(f"{self.source_subscription}\0{ident}".encode()).hexdigest()


MAX_STORED_DEAD_LETTER_BYTES = 64 * 1024

_MARK_PROCESSED = text(
    "INSERT INTO processed_events (consumer, event_id, event_type, aggregate_kind, "
    "aggregate_key, trace_id, correlation_id, delivery_attempt) "
    "VALUES (:consumer, :event_id, :event_type, :kind, :key, :trace_id, :correlation_id, "
    ":attempt) ON CONFLICT DO NOTHING RETURNING event_id"
)
_LOG_EVENT = text(
    "INSERT INTO entity_events (event_id, aggregate_kind, aggregate_key, event_type, entity_id, "
    "occurred_at, payload, trace_id, correlation_id, causation_id) VALUES (:event_id, :kind, "
    ":key, :event_type, :entity_id, :occurred_at, CAST(:payload AS JSONB), :trace_id, "
    ":correlation_id, :causation_id) ON CONFLICT (event_id) DO NOTHING"
)
_LOAD_EVENTS = text(
    "SELECT event_id::text, event_type, entity_id, occurred_at, payload FROM entity_events "
    "WHERE aggregate_kind = :kind AND aggregate_key = :key"
)
_UPSERT_CUSTOMER = text(
    "INSERT INTO customers (customer_id, state, region_id, industry, tier, is_existing, "
    "churn_reason, applied_count, pending_count, last_applied_at) VALUES (:customer_id, "
    ":state, :region_id, :industry, :tier, :is_existing, :churn_reason, :applied, :pending, "
    ":last_applied_at) ON CONFLICT (customer_id) DO UPDATE SET state = EXCLUDED.state, "
    "tier = EXCLUDED.tier, churn_reason = EXCLUDED.churn_reason, "
    "applied_count = EXCLUDED.applied_count, pending_count = EXCLUDED.pending_count, "
    "last_applied_at = EXCLUDED.last_applied_at, version = customers.version + 1, "
    "updated_at = now()"
)
_UPSERT_SUBSCRIPTION = text(
    "INSERT INTO subscriptions (customer_id, state, products, base_fee_minor, "
    "billing_period_days) VALUES (:customer_id, :state, :products, :fee, :period) "
    "ON CONFLICT (customer_id) DO UPDATE SET state = EXCLUDED.state, "
    "products = EXCLUDED.products, base_fee_minor = EXCLUDED.base_fee_minor, "
    "billing_period_days = EXCLUDED.billing_period_days, updated_at = now()"
)
_UPSERT_INVOICE = text(
    "INSERT INTO invoices (invoice_id, customer_id, state, amount_minor, currency, "
    "period_start, period_end, attempts, amount_paid_minor, last_failure_reason, "
    "applied_count, pending_count, last_applied_at) VALUES (:invoice_id, :customer_id, :state, "
    ":amount, :currency, CAST(:period_start AS DATE), CAST(:period_end AS DATE), :attempts, "
    ":paid, :failure, :applied, :pending, :last_applied_at) ON CONFLICT (invoice_id) DO UPDATE "
    "SET state = EXCLUDED.state, attempts = EXCLUDED.attempts, "
    "amount_paid_minor = EXCLUDED.amount_paid_minor, "
    "last_failure_reason = EXCLUDED.last_failure_reason, "
    "applied_count = EXCLUDED.applied_count, pending_count = EXCLUDED.pending_count, "
    "last_applied_at = EXCLUDED.last_applied_at, version = invoices.version + 1, "
    "updated_at = now()"
)
_INSERT_LEDGER = text(
    "INSERT INTO payment_ledger (invoice_id, customer_id, amount_minor, currency, event_id, "
    "paid_at) VALUES (:invoice_id, :customer_id, :amount, :currency, :event_id, :paid_at) "
    "ON CONFLICT DO NOTHING"
)
_INSERT_TRANSITION = text(
    "INSERT INTO state_transitions (machine, aggregate_key, event_id, from_state, to_state, "
    "occurred_at) VALUES (:machine, :key, :event_id, :from_state, :to_state, :occurred_at) "
    "ON CONFLICT DO NOTHING"
)
_INSERT_DEAD_LETTER = text(
    "INSERT INTO dead_letters (dead_letter_key, source_subscription, reason, detail, event_id, "
    "event_type, trace_id, correlation_id, delivery_attempt, data_sha256, data_size, data) "
    "VALUES (:key, :sub, :reason, :detail, :event_id, :event_type, :trace_id, :correlation_id, "
    ":attempt, :sha, :size, :data) ON CONFLICT DO NOTHING RETURNING dead_letter_key"
)


class ControlPlaneStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # --- writes -----------------------------------------------------------------------
    def apply_event(self, consumer: str, ev: EventRecord) -> ApplyResult:
        aggregate = aggregate_of(ev.event_type, ev.entity_id, ev.payload)
        if aggregate is None:
            # Not control-plane state (normally filtered out by the subscription). No write,
            # so it is trivially idempotent and never grows Postgres.
            return ApplyResult(ApplyStatus.IGNORED)
        kind, key = aggregate
        with transient_errors(), self.engine.begin() as conn:
            marked = conn.execute(
                _MARK_PROCESSED,
                {
                    "consumer": consumer,
                    "event_id": ev.event_id,
                    "event_type": ev.event_type,
                    "kind": kind.value,
                    "key": key,
                    "trace_id": ev.trace_id,
                    "correlation_id": ev.correlation_id,
                    "attempt": ev.delivery_attempt,
                },
            ).first()
            if marked is None:
                return ApplyResult(ApplyStatus.DUPLICATE, aggregate)
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock, 0))"),
                {"lock": f"{kind.value}:{key}"},
            )
            conn.execute(
                _LOG_EVENT,
                {
                    "event_id": ev.event_id,
                    "kind": kind.value,
                    "key": key,
                    "event_type": ev.event_type,
                    "entity_id": ev.entity_id,
                    "occurred_at": ev.occurred_at,
                    "payload": json.dumps(dict(ev.payload), sort_keys=True),
                    "trace_id": ev.trace_id,
                    "correlation_id": ev.correlation_id,
                    "causation_id": ev.causation_id,
                },
            )
            pending = self._refold(conn, kind, key)
        return ApplyResult(ApplyStatus.APPLIED, aggregate, pending)

    def _refold(self, conn: Connection, kind: AggregateKind, key: str) -> int:
        events = [
            LoggedEvent(str(r[0]), r[1], r[2], r[3], r[4])
            for r in conn.execute(_LOAD_EVENTS, {"kind": kind.value, "key": key})
        ]
        if kind is AggregateKind.CUSTOMER:
            customer = fold_customer(key, events)
            self._write_customer(conn, customer)
            self._write_transitions(conn, key, customer.transitions)
            return len(customer.pending)
        invoice = fold_invoice(key, events)
        self._write_invoice(conn, invoice)
        self._write_transitions(conn, key, invoice.transitions)
        return len(invoice.pending)

    @staticmethod
    def _write_customer(conn: Connection, p: CustomerProjection) -> None:
        if p.state is None:
            return  # nothing applicable yet (e.g. lifecycle events before customer.created)
        conn.execute(
            _UPSERT_CUSTOMER,
            {
                "customer_id": p.customer_id,
                "state": p.state.value,
                "region_id": p.region_id,
                "industry": p.industry,
                "tier": p.tier,
                "is_existing": p.is_existing,
                "churn_reason": p.churn_reason,
                "applied": len(p.applied),
                "pending": len(p.pending),
                "last_applied_at": p.last_applied_at,
            },
        )
        if p.subscription_state is not None:
            conn.execute(
                _UPSERT_SUBSCRIPTION,
                {
                    "customer_id": p.customer_id,
                    "state": p.subscription_state.value,
                    "products": list(p.products),
                    "fee": p.base_fee_minor,
                    "period": p.billing_period_days,
                },
            )

    @staticmethod
    def _write_invoice(conn: Connection, p: InvoiceProjection) -> None:
        if p.state is None:
            return  # payment events before invoice.created stay pending in the log
        conn.execute(
            _UPSERT_INVOICE,
            {
                "invoice_id": p.invoice_id,
                "customer_id": p.customer_id,
                "state": p.state.value,
                "amount": p.amount_minor,
                "currency": p.currency,
                "period_start": p.period_start,
                "period_end": p.period_end,
                "attempts": p.attempts,
                "paid": p.amount_paid_minor,
                "failure": p.last_failure_reason,
                "applied": len(p.applied),
                "pending": len(p.pending),
                "last_applied_at": p.last_applied_at,
            },
        )
        if p.state is InvoiceState.PAID:
            conn.execute(
                _INSERT_LEDGER,
                {
                    "invoice_id": p.invoice_id,
                    "customer_id": p.customer_id,
                    "amount": p.amount_paid_minor,
                    "currency": p.currency,
                    "event_id": p.paid_event_id,
                    "paid_at": p.paid_at,
                },
            )

    @staticmethod
    def _write_transitions(conn: Connection, key: str, transitions: tuple[Transition, ...]) -> None:
        if not transitions:
            return
        conn.execute(
            _INSERT_TRANSITION,
            [
                {
                    "machine": t.machine.value,
                    "key": key,
                    "event_id": t.event_id,
                    "from_state": t.from_state,
                    "to_state": t.to_state,
                    "occurred_at": t.occurred_at,
                }
                for t in transitions
            ],
        )

    def record_dead_letter(self, dl: DeadLetterRecord) -> bool:
        """Returns True if this dead letter is new."""
        with transient_errors(), self.engine.begin() as conn:
            row = conn.execute(
                _INSERT_DEAD_LETTER,
                {
                    "key": dl.key,
                    "sub": dl.source_subscription,
                    "reason": dl.reason,
                    "detail": dl.detail,
                    "event_id": dl.event_id,
                    "event_type": dl.event_type,
                    "trace_id": dl.trace_id,
                    "correlation_id": dl.correlation_id,
                    "attempt": dl.delivery_attempt,
                    "sha": dl.data_sha256,
                    "size": len(dl.data),
                    "data": dl.data if len(dl.data) <= MAX_STORED_DEAD_LETTER_BYTES else None,
                },
            ).first()
        return row is not None

    def dead_letters_for_redrive(
        self, *, reason: str | None = None, limit: int = 1000
    ) -> list[tuple[str, bytes]]:
        """(key, original bytes) of stored, not-yet-redriven dead letters."""
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT dead_letter_key, data FROM dead_letters WHERE redriven_at IS NULL "
                    "AND data IS NOT NULL AND (CAST(:reason AS TEXT) IS NULL OR reason = :reason) "
                    "ORDER BY first_seen_at, dead_letter_key LIMIT :limit"
                ),
                {"reason": reason, "limit": limit},
            )
            return [(str(r[0]), bytes(r[1])) for r in rows]

    def mark_redriven(self, keys: list[str]) -> int:
        if not keys:
            return 0
        with transient_errors(), self.engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE dead_letters SET redriven_at = now() "
                    "WHERE dead_letter_key = ANY(:keys) AND redriven_at IS NULL"
                ),
                {"keys": keys},
            )
            return int(result.rowcount)

    # --- reads ------------------------------------------------------------------------
    def dead_letter_counts(self) -> dict[str, int]:
        """Open (not redriven) dead letters by reason."""
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT reason, count(*) FROM dead_letters WHERE redriven_at IS NULL "
                    "GROUP BY reason ORDER BY reason"
                )
            )
            return {str(r[0]): int(r[1]) for r in rows}

    def dead_letter_breakdown(self) -> dict[str, dict[str, int]]:
        """Open dead letters by source subscription, then reason."""
        out: dict[str, dict[str, int]] = {}
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT source_subscription, reason, count(*) FROM dead_letters "
                    "WHERE redriven_at IS NULL GROUP BY 1, 2 ORDER BY 1, 2"
                )
            )
            for sub, reason, n in rows:
                out.setdefault(str(sub), {})[str(reason)] = int(n)
        return out

    def processed_for_correlation(self, correlation_id: str) -> list[dict[str, Any]]:
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT consumer, event_id::text, event_type, trace_id, correlation_id "
                    "FROM processed_events WHERE correlation_id = :cid ORDER BY event_id"
                ),
                {"cid": correlation_id},
            )
            return [dict(r._mapping) for r in rows]

    def pending_summary(self) -> dict[str, int]:
        """Events that are logged but not applicable yet (waiting for a predecessor)."""
        with transient_errors(), self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT (SELECT coalesce(sum(pending_count), 0) FROM customers), "
                    "(SELECT coalesce(sum(pending_count), 0) FROM invoices), "
                    "(SELECT count(*) FROM entity_events e WHERE aggregate_kind = 'customer' "
                    " AND NOT EXISTS (SELECT 1 FROM customers c WHERE c.customer_id = "
                    " e.aggregate_key)), "
                    "(SELECT count(*) FROM entity_events e WHERE aggregate_kind = 'invoice' "
                    " AND NOT EXISTS (SELECT 1 FROM invoices i WHERE i.invoice_id = "
                    " e.aggregate_key))"
                )
            ).one()
        return {
            "customer_pending": int(row[0]),
            "invoice_pending": int(row[1]),
            "customer_orphans": int(row[2]),
            "invoice_orphans": int(row[3]),
        }

    def snapshot(self) -> dict[str, Any]:
        """Business state, excluding bookkeeping timestamps. Used to compare runs."""
        with transient_errors(), self.engine.connect() as conn:
            customers = {
                r[0]: list(r[1:])
                for r in conn.execute(
                    text(
                        "SELECT c.customer_id, c.state, c.tier, c.region_id, c.churn_reason, "
                        "c.pending_count, s.state, s.products, s.base_fee_minor "
                        "FROM customers c LEFT JOIN subscriptions s USING (customer_id)"
                    )
                )
            }
            invoices = {
                r[0]: list(r[1:])
                for r in conn.execute(
                    text(
                        "SELECT invoice_id, customer_id, state, amount_minor, attempts, "
                        "amount_paid_minor, pending_count FROM invoices"
                    )
                )
            }
            ledger = conn.execute(
                text("SELECT count(*), coalesce(sum(amount_minor), 0) FROM payment_ledger")
            ).one()
            transitions = conn.execute(text("SELECT count(*) FROM state_transitions")).scalar_one()
        return {
            "customers": dict(sorted(customers.items())),
            "invoices": dict(sorted(invoices.items())),
            "ledger_entries": int(ledger[0]),
            "ledger_total_minor": int(ledger[1]),
            "transitions": int(transitions),
        }


def snapshot_checksum(snapshot: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()
