"""Dunning service: payment events -> decision -> access stage -> scheduled retry.

One transaction per event (consumer ``dunning``): idempotency mark, invoice lock, case
update, decision record, stage transition and retry-job row commit together or not at all.
Queue side effects (create / delete Cloud Tasks) happen AFTER the commit, from the committed
rows (transactional outbox): a crash in between leaves a job with ``enqueued_at IS NULL``,
which ``flush_outbox`` enqueues later under the same task name (``ALREADY_EXISTS`` = done).

Event handling (order-tolerant, duplicate-safe):

* ``payment.failed`` attempt n: opens the case (n = 1 sets elapsed time zero) or advances it.
  A failure no newer than the attempts already seen is stale and only refines ``opened_at``.
  Any SCHEDULED job is superseded, the policy decides again, the stage moves, and a new job
  is created when the decision is to retry.
* ``payment.succeeded`` attempt n >= 2 (a recovery): the case becomes RECOVERED (created as
  such when the success arrives first) and every SCHEDULED job is cancelled.
* ``churn.observed``: open cases of the customer are CLOSED and their jobs cancelled.

A recovered invoice can never be charged again by a stale job: cancellation here, plus the
executor's re-check of the case and of the provider's invoice right before charging.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine

from praxis.control.db import transient_errors
from praxis.domain.dunning import (
    DECISION_TRIGGER,
    DUNNING_TRANSITIONS,
    TERMINAL_DUNNING_STATES,
    DunningState,
)
from praxis.domain.dunning import RetryJobState as JS
from praxis.dunning.store import Case, DunningRepo, Job
from praxis.dunning.tasks import RetryTask, TaskQueue
from praxis.payments.ids import idempotency_key
from praxis.recovery.dataset import DAY_S
from praxis.recovery.features import Attempt, CustomerHistory, RecoveryFeatures, episode_features
from praxis.recovery.policy import DecisionContext, RecoveryDecider, RecoveryDecision

logger = logging.getLogger(__name__)
CONSUMER = "dunning"
DECISION_NS = uuid.UUID("0f7d3a4e-5b8c-4f1e-9a2d-6c3b7e8f1a24")
RELEVANT = frozenset({"payment.failed", "payment.succeeded", "churn.observed"})


@dataclass(frozen=True, slots=True)
class DunningEvent:
    event_id: str
    event_type: str
    entity_id: str  # customer id
    occurred_at: datetime
    payload: dict[str, Any]
    trace_id: str
    correlation_id: str
    provider: str | None = None  # payment provider of the invoice, None = not chargeable
    delivery_attempt: int = 1


@dataclass
class Outcome:
    status: str  # applied | duplicate | ignored | stale
    invoice_id: str | None = None
    state: DunningState | None = None
    decision: RecoveryDecision | None = None
    enqueue: list[Job] = field(default_factory=list)
    cancel: list[str] = field(default_factory=list)  # task names


def _utcnow() -> datetime:
    return datetime.now(UTC)


def task_name(invoice_id: str, attempt: int, decision_id: str) -> str:
    digest = hashlib.sha256(f"{invoice_id}\0{attempt}\0{decision_id}".encode()).hexdigest()
    return f"retry-{digest[:40]}"


def retry_key(invoice_id: str, attempt: int) -> str:
    """One idempotency key per (invoice, attempt): any re-dispatch reuses it."""
    return idempotency_key("retry_invoice", invoice_id, f"dunning-attempt-{attempt}")


def features_for(repo: DunningRepo, case: Case) -> RecoveryFeatures | None:
    """``recovery_features.v1`` from the control plane; None when the facts are missing."""
    history = repo.history(case.customer_id, before=case.opened_at)
    if history is None or case.first_reason is None:
        return None
    current = next((i for i in history.invoices if i.invoice_id == case.invoice_id), None)
    if current is None:
        return None
    first = Attempt(1, case.opened_at, False, case.first_reason)
    invoices = tuple(
        replace(i, attempts=(first,)) if i.invoice_id == case.invoice_id else i
        for i in history.invoices
    )
    merged: CustomerHistory = replace(history, invoices=invoices)
    return episode_features(merged, case.invoice_id)


class DunningService:
    def __init__(
        self,
        engine: Engine,
        decider: RecoveryDecider,
        queue: TaskQueue,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.engine = engine
        self.decider = decider
        self.queue = queue
        self.clock = clock

    # ----------------------------------------------------------------- events
    def handle(self, event: DunningEvent) -> Outcome:
        if event.event_type not in RELEVANT:
            return Outcome("ignored")
        with transient_errors(), self.engine.begin() as conn:
            repo = DunningRepo(conn)
            fresh = repo.mark_processed(
                CONSUMER,
                {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "key": event.payload.get("invoice_id", event.entity_id),
                    "trace_id": event.trace_id,
                    "correlation_id": event.correlation_id,
                    "attempt": event.delivery_attempt,
                },
            )
            if not fresh:
                return Outcome("duplicate")
            if event.event_type == "payment.failed":
                outcome = self._failed(repo, event)
            elif event.event_type == "payment.succeeded":
                outcome = self._succeeded(repo, event)
            else:
                outcome = self._churned(repo, event)
        self._after_commit(outcome)
        return outcome

    def _failed(self, repo: DunningRepo, ev: DunningEvent) -> Outcome:
        p = ev.payload
        invoice, n = str(p["invoice_id"]), int(p["attempt_number"])
        repo.lock_invoice(invoice)
        case = repo.case(invoice, for_update=True)
        if case is None:
            case = Case(
                invoice,
                ev.entity_id,
                ev.provider,
                DunningState.PAST_DUE,
                int(p["amount_minor"]),
                str(p["currency"]),
                p.get("reason") if n == 1 else None,
                ev.occurred_at,
                n,
                ev.occurred_at,
            )
            repo.insert_case(case)
            repo.audit("dunning", invoice, ev.event_id, None, case.state.value, ev.occurred_at)
        elif case.state in TERMINAL_DUNNING_STATES or n <= case.attempts_seen:
            if n == 1 and ev.occurred_at < case.opened_at:  # late first failure: true time zero
                repo.update_case_progress(
                    invoice,
                    attempts_seen=case.attempts_seen,
                    opened_at=ev.occurred_at,
                    last_failure_at=case.last_failure_at,
                    first_reason=p.get("reason"),
                )
            return Outcome("stale", invoice, case.state)
        else:
            opened = min(case.opened_at, ev.occurred_at) if n == 1 else case.opened_at
            reason = p.get("reason") if n == 1 else None
            repo.update_case_progress(
                invoice,
                attempts_seen=n,
                opened_at=opened,
                last_failure_at=ev.occurred_at,
                first_reason=reason,
            )
            case = replace(
                case,
                attempts_seen=n,
                opened_at=opened,
                last_failure_at=ev.occurred_at,
                first_reason=case.first_reason or reason,
            )
        outcome = Outcome("applied", invoice)
        outcome.cancel = self._retire_scheduled(repo, invoice, "job.superseded")
        return self._decide(repo, case, ev, outcome)

    def _decide(self, repo: DunningRepo, case: Case, ev: DunningEvent, out: Outcome) -> Outcome:
        features = features_for(repo, case)
        elapsed = (ev.occurred_at - case.opened_at).total_seconds() / DAY_S
        decision = self.decider.decide(
            DecisionContext(
                features=features,
                attempts_made=case.attempts_seen,
                now_elapsed_days=max(0.0, elapsed),
                last_attempt_elapsed_days=max(0.0, elapsed),
                amount_minor=case.amount_minor,
                decided_at=ev.occurred_at,
                current_stage=case.state,
            )
        )
        decision_id = str(uuid.uuid5(DECISION_NS, f"{case.invoice_id}:{ev.event_id}"))
        retry_at = (
            case.opened_at + timedelta(days=decision.retry_elapsed_days)
            if decision.retry_elapsed_days is not None
            else None
        )
        repo.insert_decision(
            {
                "decision_id": decision_id,
                "invoice_id": case.invoice_id,
                "attempts_made": case.attempts_seen,
                "decided_at": ev.occurred_at,
                "action": decision.action,
                "retry_at": retry_at,
                "stage": decision.stage.value,
                "policy_kind": decision.policy_kind.value,
                "policy_version": decision.policy_version,
                "model_version": decision.model_version,
                "fallback_reason": decision.fallback_reason,
                "expected_value_minor": decision.expected_value_minor,
                "p_next_success": decision.p_next_success,
                "plan": {"retry_days": list(decision.planned_retry_days)},
                "features": asdict(features) if features is not None else None,
                "trace_id": ev.trace_id,
                "event_id": ev.event_id,
            }
        )
        state, trigger = case.state, DECISION_TRIGGER[decision.stage]
        if trigger in DUNNING_TRANSITIONS.allowed(state):  # SUSPENDED stays SUSPENDED
            state = repo.move_case(case, trigger, event_id=decision_id, occurred_at=ev.occurred_at)
        if retry_at is not None:
            attempt = case.attempts_seen + 1
            job = Job(
                job_id=f"job-{decision_id}",
                invoice_id=case.invoice_id,
                decision_id=decision_id,
                attempt_number=attempt,
                run_at=retry_at,
                task_name=task_name(case.invoice_id, attempt, decision_id),
                idempotency_key=retry_key(case.invoice_id, attempt),
                state=JS.SCHEDULED,
                enqueued_at=None,
                dispatches=0,
            )
            repo.insert_job(job)
            out.enqueue.append(job)
        out.state, out.decision = state, decision
        logger.info(
            "dunning.decided",
            extra={
                "invoice_id": case.invoice_id,
                "decision_id": decision_id,
                "action": decision.action,
                "stage": state.value,
                "policy_kind": decision.policy_kind.value,
                "policy_version": decision.policy_version,
                "model_version": decision.model_version,
                "fallback_reason": decision.fallback_reason,
                "trace_id": ev.trace_id,
            },
        )
        return out

    def _succeeded(self, repo: DunningRepo, ev: DunningEvent) -> Outcome:
        p = ev.payload
        invoice, n = str(p["invoice_id"]), int(p["attempt_number"])
        if n < 2:
            return Outcome("ignored", invoice)  # paid on the first attempt: never in dunning
        repo.lock_invoice(invoice)
        case = repo.case(invoice, for_update=True)
        if case is None:
            case = Case(
                invoice,
                ev.entity_id,
                ev.provider,
                DunningState.RECOVERED,
                int(p["amount_minor"]),
                str(p["currency"]),
                None,
                ev.occurred_at,
                n,
                None,
            )
            repo.insert_case(case)
            repo.audit("dunning", invoice, ev.event_id, None, case.state.value, ev.occurred_at)
            return Outcome("applied", invoice, case.state)
        if case.state in TERMINAL_DUNNING_STATES:
            return Outcome("stale", invoice, case.state)
        out = Outcome("applied", invoice)
        out.cancel = self._retire_scheduled(repo, invoice, "job.cancelled")
        out.state = repo.move_case(
            case, "payment.recovered", event_id=ev.event_id, occurred_at=ev.occurred_at
        )
        return out

    def _churned(self, repo: DunningRepo, ev: DunningEvent) -> Outcome:
        out = Outcome("applied")
        for case in repo.open_cases_of(ev.entity_id):
            repo.lock_invoice(case.invoice_id)
            out.cancel += self._retire_scheduled(repo, case.invoice_id, "job.cancelled")
            repo.move_case(
                case,
                "subscription.cancelled",
                event_id=ev.event_id,
                occurred_at=ev.occurred_at,
                closed_reason=str(ev.payload.get("reason")),
            )
        return out

    def _retire_scheduled(self, repo: DunningRepo, invoice_id: str, trigger: str) -> list[str]:
        """Supersede / cancel SCHEDULED jobs (an EXECUTING job must finish and be recorded)."""
        names = []
        for job in repo.live_jobs(invoice_id):
            if job.state is JS.SCHEDULED:
                repo.move_job(job, trigger, now=self.clock())
                names.append(job.task_name)
        return names

    # ---------------------------------------------------------------- outbox
    def _after_commit(self, outcome: Outcome) -> None:
        for name in outcome.cancel:
            self._cancel_task(name)
        for job in outcome.enqueue:
            self._enqueue(job)

    def _cancel_task(self, name: str) -> None:
        # Best effort: a task that survives is a no-op at execution (job no longer scheduled).
        try:
            self.queue.cancel(name)
        except Exception:
            logger.warning("dunning.cancel_failed", extra={"task_name": name}, exc_info=True)

    def _enqueue(self, job: Job) -> bool:
        try:
            self.queue.enqueue(RetryTask(job.task_name, job.job_id, job.run_at))
        except Exception:
            logger.warning("dunning.enqueue_failed", extra={"job_id": job.job_id}, exc_info=True)
            return False
        with transient_errors(), self.engine.begin() as conn:
            DunningRepo(conn).mark_enqueued(job.job_id, self.clock())
        return True

    def flush_outbox(self, limit: int = 100) -> int:
        """Enqueue committed jobs whose task creation did not happen (crash, queue outage)."""
        with transient_errors(), self.engine.begin() as conn:
            pending = DunningRepo(conn).unenqueued(limit)
        return sum(self._enqueue(job) for job in pending)

    # ---------------------------------------------------------------- re-plan
    def replan(self, invoice_id: str, *, trace_id: str) -> Outcome:
        """Decide again now (an expired job): same policy, elapsed time = now."""
        now = self.clock()
        with transient_errors(), self.engine.begin() as conn:
            repo = DunningRepo(conn)
            repo.lock_invoice(invoice_id)
            case = repo.case(invoice_id, for_update=True)
            if case is None or case.state in TERMINAL_DUNNING_STATES:
                return Outcome("stale", invoice_id, case.state if case else None)
            out = Outcome("applied", invoice_id)
            out.cancel = self._retire_scheduled(repo, invoice_id, "job.superseded")
            synthetic = DunningEvent(
                event_id=str(uuid.uuid5(DECISION_NS, f"replan:{invoice_id}:{now.isoformat()}")),
                event_type="dunning.replan",
                entity_id=case.customer_id,
                occurred_at=now,
                payload={},
                trace_id=trace_id,
                correlation_id=trace_id,
            )
            out = self._decide(repo, case, synthetic, out)
        self._after_commit(out)
        return out
