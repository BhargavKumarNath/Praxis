"""Postgres access for dunning: cases, decisions, retry jobs, history (migration 0004).

All writes go through a caller-owned connection, so the dunning service decides what one
transaction contains. Every state change is validated twice: in Python against the domain
machine (``praxis.domain.dunning``) and in Postgres by the state-guard trigger.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, text

from praxis.domain.dunning import (
    DUNNING_TRANSITIONS,
    RETRY_JOB_TRANSITIONS,
    DunningState,
    RetryJobState,
)
from praxis.recovery.features import Attempt, CustomerHistory, InvoiceRecord


@dataclass(frozen=True, slots=True)
class Case:
    invoice_id: str
    customer_id: str
    provider: str | None
    state: DunningState
    amount_minor: int
    currency: str
    first_reason: str | None
    opened_at: datetime
    attempts_seen: int
    last_failure_at: datetime | None


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    invoice_id: str
    decision_id: str
    attempt_number: int
    run_at: datetime
    task_name: str
    idempotency_key: str
    state: RetryJobState
    enqueued_at: datetime | None
    dispatches: int


_CASE_COLS = (
    "invoice_id, customer_id, provider, state, amount_minor, currency, first_reason, "
    "opened_at, attempts_seen, last_failure_at"
)
_JOB_COLS = (
    "job_id, invoice_id, decision_id, attempt_number, run_at, task_name, idempotency_key, "
    "state, enqueued_at, dispatches"
)


def _case(row: Sequence[Any]) -> Case:
    return Case(
        invoice_id=row[0],
        customer_id=row[1],
        provider=row[2],
        state=DunningState(row[3]),
        amount_minor=int(row[4]),
        currency=row[5],
        first_reason=row[6],
        opened_at=row[7],
        attempts_seen=int(row[8]),
        last_failure_at=row[9],
    )


def _job(row: Sequence[Any]) -> Job:
    return Job(
        job_id=row[0],
        invoice_id=row[1],
        decision_id=row[2],
        attempt_number=int(row[3]),
        run_at=row[4],
        task_name=row[5],
        idempotency_key=row[6],
        state=RetryJobState(row[7]),
        enqueued_at=row[8],
        dispatches=int(row[9]),
    )


class DunningRepo:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------- locking
    def lock_invoice(self, invoice_id: str) -> None:
        """Serialise everything that touches one invoice's dunning (consumer, executor)."""
        self.conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"dunning:{invoice_id}"},
        )

    def mark_processed(self, consumer: str, record: Mapping[str, Any]) -> bool:
        """Idempotency: False when this consumer already applied the event."""
        row = self.conn.execute(
            text(
                "INSERT INTO processed_events (consumer, event_id, event_type, aggregate_kind, "
                "aggregate_key, trace_id, correlation_id, delivery_attempt) VALUES (:consumer, "
                ":event_id, :event_type, 'dunning', :key, :trace_id, :correlation_id, :attempt) "
                "ON CONFLICT DO NOTHING RETURNING event_id"
            ),
            {"consumer": consumer, **record},
        ).first()
        return row is not None

    # --------------------------------------------------------------------- cases
    def case(self, invoice_id: str, *, for_update: bool = False) -> Case | None:
        lock = " FOR UPDATE" if for_update else ""
        row = self.conn.execute(
            text(f"SELECT {_CASE_COLS} FROM dunning_cases WHERE invoice_id = :i{lock}"),  # noqa: S608
            {"i": invoice_id},
        ).first()
        return _case(row) if row else None

    def open_cases_of(self, customer_id: str) -> list[Case]:
        rows = self.conn.execute(
            text(
                f"SELECT {_CASE_COLS} FROM dunning_cases WHERE customer_id = :c "  # noqa: S608
                "AND state NOT IN ('recovered', 'closed') ORDER BY invoice_id FOR UPDATE"
            ),
            {"c": customer_id},
        ).all()
        return [_case(r) for r in rows]

    def insert_case(self, case: Case) -> None:
        self.conn.execute(
            text(
                f"INSERT INTO dunning_cases ({_CASE_COLS}, recovered_at) VALUES (:invoice_id, "  # noqa: S608
                ":customer_id, :provider, :state, :amount_minor, :currency, :first_reason, "
                ":opened_at, :attempts_seen, :last_failure_at, :recovered_at)"
            ),
            {
                **{f: getattr(case, f) for f in Case.__slots__},
                "state": case.state.value,
                "recovered_at": case.opened_at if case.state is DunningState.RECOVERED else None,
            },
        )

    def update_case_progress(
        self,
        invoice_id: str,
        *,
        attempts_seen: int,
        opened_at: datetime,
        last_failure_at: datetime | None,
        first_reason: str | None,
    ) -> None:
        self.conn.execute(
            text(
                "UPDATE dunning_cases SET attempts_seen = :a, opened_at = :o, "
                "last_failure_at = :l, first_reason = coalesce(first_reason, :r), "
                "version = version + 1, updated_at = now() WHERE invoice_id = :i"
            ),
            {
                "a": attempts_seen,
                "o": opened_at,
                "l": last_failure_at,
                "r": first_reason,
                "i": invoice_id,
            },
        )

    def move_case(
        self,
        case: Case,
        trigger: str,
        *,
        event_id: str,
        occurred_at: datetime,
        closed_reason: str | None = None,
    ) -> DunningState:
        new = DUNNING_TRANSITIONS.apply(case.state, trigger)
        self.conn.execute(
            text(
                "UPDATE dunning_cases SET state = :s, version = version + 1, updated_at = now(), "
                "recovered_at = CASE WHEN :s = 'recovered' THEN :t ELSE recovered_at END, "
                "closed_reason = coalesce(:cr, closed_reason) WHERE invoice_id = :i"
            ),
            {"s": new.value, "t": occurred_at, "cr": closed_reason, "i": case.invoice_id},
        )
        self.audit("dunning", case.invoice_id, event_id, case.state.value, new.value, occurred_at)
        return new

    def audit(
        self,
        machine: str,
        key: str,
        event_id: str,
        from_state: str | None,
        to_state: str,
        occurred_at: datetime,
    ) -> None:
        self.conn.execute(
            text(
                "INSERT INTO state_transitions (machine, aggregate_key, event_id, from_state, "
                "to_state, occurred_at) VALUES (:m, :k, :e, :f, :t, :o) ON CONFLICT DO NOTHING"
            ),
            {
                "m": machine,
                "k": key,
                "e": event_id,
                "f": from_state,
                "t": to_state,
                "o": occurred_at,
            },
        )

    # ----------------------------------------------------------------- decisions
    def insert_decision(self, row: Mapping[str, Any]) -> None:
        self.conn.execute(
            text(
                "INSERT INTO dunning_decisions (decision_id, invoice_id, attempts_made, "
                "decided_at, action, retry_at, stage, policy_kind, policy_version, "
                "model_version, fallback_reason, expected_value_minor, p_next_success, plan, "
                "features, trace_id, event_id) VALUES (:decision_id, :invoice_id, "
                ":attempts_made, :decided_at, :action, :retry_at, :stage, :policy_kind, "
                ":policy_version, :model_version, :fallback_reason, :expected_value_minor, "
                ":p_next_success, CAST(:plan AS JSONB), CAST(:features AS JSONB), :trace_id, "
                ":event_id) ON CONFLICT (decision_id) DO NOTHING"
            ),
            {**row, "plan": json.dumps(row["plan"]), "features": json.dumps(row["features"])},
        )

    def decisions(self, invoice_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            text(
                "SELECT decision_id, attempts_made, action, retry_at, stage, policy_kind, "
                "model_version, fallback_reason FROM dunning_decisions WHERE invoice_id = :i "
                "ORDER BY decided_at, decision_id"
            ),
            {"i": invoice_id},
        ).mappings()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------------- jobs
    def insert_job(self, job: Job) -> None:
        self.conn.execute(
            text(
                f"INSERT INTO retry_jobs ({_JOB_COLS}) VALUES (:job_id, :invoice_id, "  # noqa: S608
                ":decision_id, :attempt_number, :run_at, :task_name, :idempotency_key, :state, "
                ":enqueued_at, :dispatches)"
            ),
            {**{f: getattr(job, f) for f in Job.__slots__}, "state": job.state.value},
        )

    def job(self, job_id: str, *, for_update: bool = False) -> Job | None:
        lock = " FOR UPDATE" if for_update else ""
        row = self.conn.execute(
            text(f"SELECT {_JOB_COLS} FROM retry_jobs WHERE job_id = :j{lock}"),  # noqa: S608
            {"j": job_id},
        ).first()
        return _job(row) if row else None

    def live_jobs(self, invoice_id: str) -> list[Job]:
        rows = self.conn.execute(
            text(
                f"SELECT {_JOB_COLS} FROM retry_jobs WHERE invoice_id = :i "  # noqa: S608
                "AND state IN ('scheduled', 'executing') ORDER BY created_at FOR UPDATE"
            ),
            {"i": invoice_id},
        ).all()
        return [_job(r) for r in rows]

    def jobs(self, invoice_id: str) -> list[Job]:
        rows = self.conn.execute(
            text(
                f"SELECT {_JOB_COLS} FROM retry_jobs WHERE invoice_id = :i "  # noqa: S608
                "ORDER BY created_at, job_id"
            ),
            {"i": invoice_id},
        ).all()
        return [_job(r) for r in rows]

    def move_job(
        self,
        job: Job,
        trigger: str,
        *,
        now: datetime,
        outcome: str | None = None,
        failure_reason: str | None = None,
    ) -> RetryJobState:
        new = RETRY_JOB_TRANSITIONS.apply(job.state, trigger)
        self.conn.execute(
            text(
                "UPDATE retry_jobs SET state = :s, updated_at = now(), "
                "started_at = CASE WHEN :s = 'executing' THEN :now ELSE started_at END, "
                "finished_at = CASE WHEN :s IN ('executing', 'scheduled') THEN finished_at "
                "ELSE :now END, outcome = coalesce(:o, outcome), "
                "failure_reason = coalesce(:fr, failure_reason) WHERE job_id = :j"
            ),
            {"s": new.value, "now": now, "o": outcome, "fr": failure_reason, "j": job.job_id},
        )
        return new

    def count_dispatch(self, job_id: str) -> None:
        self.conn.execute(
            text("UPDATE retry_jobs SET dispatches = dispatches + 1 WHERE job_id = :j"),
            {"j": job_id},
        )

    def mark_enqueued(self, job_id: str, now: datetime) -> None:
        self.conn.execute(
            text("UPDATE retry_jobs SET enqueued_at = coalesce(enqueued_at, :n) WHERE job_id = :j"),
            {"n": now, "j": job_id},
        )

    def unenqueued(self, limit: int) -> list[Job]:
        rows = self.conn.execute(
            text(
                f"SELECT {_JOB_COLS} FROM retry_jobs WHERE state = 'scheduled' "  # noqa: S608
                "AND enqueued_at IS NULL ORDER BY created_at LIMIT :n"
            ),
            {"n": limit},
        ).all()
        return [_job(r) for r in rows]

    # ------------------------------------------------------------------- history
    def history(self, customer_id: str, before: datetime) -> CustomerHistory | None:
        """The customer's billing history from the control plane's event log (features)."""
        rows = self.conn.execute(
            text(
                "SELECT aggregate_kind, aggregate_key, event_type, occurred_at, payload "
                "FROM entity_events WHERE entity_id = :c AND occurred_at < :b "
                "ORDER BY occurred_at, event_id"
            ),
            {"c": customer_id, "b": before},
        ).all()
        return history_from_events(customer_id, rows)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def history_from_events(customer_id: str, rows: Sequence[Sequence[Any]]) -> CustomerHistory | None:
    """Fold control-plane events into the same ``CustomerHistory`` the warehouse builds."""
    profile: dict[str, Any] | None = None
    created_at: datetime | None = None
    invoices: dict[str, dict[str, Any]] = {}
    for kind, key, event_type, occurred_at, payload in rows:
        p = payload if isinstance(payload, dict) else json.loads(payload)
        if kind == "customer" and event_type == "customer.created":
            profile, created_at = p, _utc(occurred_at)
        elif kind == "invoice" and event_type == "invoice.created":
            invoices.setdefault(key, {"attempts": []}).update(
                created_at=_utc(occurred_at), amount=int(p["amount_minor"]), tier=p["tier"]
            )
        elif kind == "invoice" and event_type in ("payment.failed", "payment.succeeded"):
            invoices.setdefault(key, {"attempts": []})["attempts"].append(
                Attempt(
                    int(p["attempt_number"]),
                    _utc(occurred_at),
                    event_type == "payment.succeeded",
                    p.get("reason"),
                )
            )
    if profile is None or created_at is None:
        return None
    records = [
        InvoiceRecord(key, v["created_at"], v["amount"], v["tier"], tuple(v["attempts"]))
        for key, v in invoices.items()
        if "created_at" in v
    ]
    return CustomerHistory(
        customer_id=customer_id,
        payment_method=profile["preferred_payment_method"],
        is_existing=bool(profile["is_existing"]),
        tenure_days_at_start=int(profile["tenure_days"]),
        created_at=created_at,
        invoices=tuple(sorted(records, key=lambda i: (i.created_at, i.invoice_id))),
    )
