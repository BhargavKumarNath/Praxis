"""Retry execution: the handler behind every scheduled retry task (at-least-once delivery).

``RetryExecutor.execute(job_id)`` is safe to call any number of times, concurrently:

1. *Guard* (one transaction, invoice lock + job row lock). A job that is no longer
   SCHEDULED or EXECUTING is a no-op (``stale``). A SCHEDULED job is cancelled when its case
   is terminal, when the provider already shows the invoice paid or the attempt already made
   (stale state: a recovered customer never gets an avoidable retry), and expired when it is
   dispatched later than ``max_job_lateness_hours`` (the service then re-plans). Otherwise it
   becomes EXECUTING and the transaction commits BEFORE the provider is called.
2. *Charge*, outside any transaction: ``BillingService.retry_invoice`` with the job's
   idempotency key. A re-dispatch of an EXECUTING job (crash, timeout, duplicate task)
   repeats the call with the same key, so the provider returns the first result instead of
   charging again.
3. *Record* SUCCEEDED / FAILED. Case state changes arrive through the provider's payment
   events (``DunningService``), never from here, so there is one source of truth.

Transient provider or database failures raise ``TransientError``: the task is re-delivered.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import Engine

from praxis.control.db import transient_errors
from praxis.domain.dunning import TERMINAL_DUNNING_STATES
from praxis.domain.dunning import RetryJobState as JS
from praxis.dunning.service import DunningService
from praxis.dunning.store import Case, DunningRepo, Job
from praxis.payments.gateway import GatewayError, PaymentGateway, SnapshotSource
from praxis.payments.model import (
    ChargeStatus,
    InvoiceSnapshot,
    InvoiceStatus,
    OutcomeStatus,
    PaymentOutcome,
)
from praxis.recovery.config import RecoveryPolicy

logger = logging.getLogger(__name__)
EARLY_TOLERANCE = timedelta(minutes=1)


def _provider_reason(snapshot: InvoiceSnapshot, job: Job) -> str | None:
    """Stale state at the provider: never charge an invoice that no longer needs it."""
    paid = snapshot.status is InvoiceStatus.PAID or any(
        c.status is ChargeStatus.SUCCEEDED for c in snapshot.charges
    )
    if paid:
        return "already_paid"
    if snapshot.status in (InvoiceStatus.VOID, InvoiceStatus.UNCOLLECTIBLE):
        return "invoice_closed"
    if len(snapshot.charges) >= job.attempt_number:
        return "attempt_already_made"
    return None


class ExecStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"  # nothing to do (cancelled, superseded, finished, unknown)
    CANCELLED = "cancelled"  # cancelled now by a guard
    EXPIRED = "expired"
    TOO_EARLY = "too_early"  # dispatched before run_at: re-deliver later


class Charger:
    """What the executor needs from billing: an idempotent invoice charge + a fresh read."""

    def __init__(self, retry: Callable[[str, str], PaymentOutcome], source: SnapshotSource) -> None:
        self.retry = retry  # (invoice_id, idempotency key) -> outcome
        self.source = source

    @classmethod
    def for_gateway(cls, gateway: PaymentGateway) -> Charger:
        def retry(invoice_id: str, key: str) -> PaymentOutcome:
            return gateway.pay_invoice(invoice_id, idempotency_key=key)

        return cls(retry, gateway)


@dataclass(frozen=True)
class ExecResult:
    status: ExecStatus
    job_id: str
    detail: str | None = None

    @property
    def done(self) -> bool:
        """True = acknowledge the task; False = let the queue re-deliver it."""
        return self.status is not ExecStatus.TOO_EARLY


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RetryExecutor:
    def __init__(
        self,
        engine: Engine,
        chargers: Mapping[str, Charger],
        policy: RecoveryPolicy,
        service: DunningService,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.engine = engine
        self.chargers = dict(chargers)
        self.policy = policy
        self.service = service
        self.clock = clock

    def handle(self, job_id: str) -> bool:
        """``TaskQueue`` handler signature."""
        return self.execute(job_id).done

    def execute(self, job_id: str) -> ExecResult:
        guard = self._guard(job_id)
        if isinstance(guard, ExecResult):
            if guard.status is ExecStatus.EXPIRED:
                self.service.replan(guard.detail or "", trace_id=f"expired:{job_id}")
            return guard
        job, provider = guard
        charger = self.chargers[provider]
        try:
            outcome = charger.retry(job.invoice_id, job.idempotency_key)
        except GatewayError as exc:  # permanent: record, never retry blindly
            return self._record(job, ExecStatus.FAILED, outcome="error", reason=exc.reason)
        if outcome.status is OutcomeStatus.SUCCEEDED:
            return self._record(job, ExecStatus.SUCCEEDED, outcome="succeeded", reason=None)
        return self._record(
            job, ExecStatus.FAILED, outcome="declined", reason=outcome.failure_reason
        )

    # ------------------------------------------------------------------ guard
    def _guard(self, job_id: str) -> ExecResult | tuple[Job, str]:
        with transient_errors(), self.engine.begin() as conn:
            repo = DunningRepo(conn)
            loaded = self._load(repo, job_id)
            if isinstance(loaded, ExecResult):
                return loaded
            job, case = loaded
            repo.count_dispatch(job_id)
            if job.state is JS.EXECUTING:  # re-dispatch after a crash: finish the same charge
                if case.provider not in self.chargers:
                    logger.error("dunning.no_gateway_for_executing_job", extra={"job_id": job_id})
                    return ExecResult(ExecStatus.STALE, job_id, "no_gateway")
                return job, str(case.provider)
            return self._admit(repo, job, case)

    @staticmethod
    def _load(repo: DunningRepo, job_id: str) -> ExecResult | tuple[Job, Case]:
        """Lock the invoice, then the job and case rows; only live jobs go further."""
        job = repo.job(job_id)
        if job is None:
            return ExecResult(ExecStatus.STALE, job_id, "unknown job")
        repo.lock_invoice(job.invoice_id)
        job = repo.job(job_id, for_update=True)
        case = repo.case(job.invoice_id, for_update=True) if job else None
        if job is None or case is None or job.state not in (JS.SCHEDULED, JS.EXECUTING):
            return ExecResult(ExecStatus.STALE, job_id, job.state.value if job else None)
        return job, case

    def _admit(self, repo: DunningRepo, job: Job, case: Case) -> ExecResult | tuple[Job, str]:
        """SCHEDULED -> EXECUTING, unless too early, stale (cancel) or too late (expire)."""
        now = self.clock()
        if now < job.run_at - EARLY_TOLERANCE:
            return ExecResult(ExecStatus.TOO_EARLY, job.job_id)
        reason = self._cancel_reason(case, job)
        if reason is not None:
            repo.move_job(job, "job.cancelled", now=now, outcome=reason)
            return ExecResult(ExecStatus.CANCELLED, job.job_id, reason)
        if now > job.run_at + timedelta(hours=self.policy.bounds.max_job_lateness_hours):
            repo.move_job(job, "job.expired", now=now, outcome="late_dispatch")
            return ExecResult(ExecStatus.EXPIRED, job.job_id, job.invoice_id)
        repo.move_job(job, "job.started", now=now)
        return job, str(case.provider)

    def _cancel_reason(self, case: Case, job: Job) -> str | None:
        if case.state in TERMINAL_DUNNING_STATES:
            return "case_closed"
        if case.provider is None or case.provider not in self.chargers:
            return "no_gateway"
        try:  # bounded-timeout provider read; TransientError -> the task is re-delivered
            snapshot = self.chargers[case.provider].source.fetch_invoice(job.invoice_id)
        except GatewayError as exc:
            return f"provider_refused:{exc.reason}"
        return _provider_reason(snapshot, job)

    # ----------------------------------------------------------------- record
    def _record(
        self, job: Job, status: ExecStatus, *, outcome: str, reason: str | None
    ) -> ExecResult:
        now = self.clock()
        with transient_errors(), self.engine.begin() as conn:
            repo = DunningRepo(conn)
            current = repo.job(job.job_id, for_update=True)
            if current is not None and current.state is JS.EXECUTING:
                trigger = "job.succeeded" if status is ExecStatus.SUCCEEDED else "job.failed"
                repo.move_job(current, trigger, now=now, outcome=outcome, failure_reason=reason)
        logger.info(
            "dunning.retry_executed",
            extra={
                "job_id": job.job_id,
                "invoice_id": job.invoice_id,
                "attempt_number": job.attempt_number,
                "outcome": outcome,
            },
        )
        return ExecResult(status, job.job_id, reason)
