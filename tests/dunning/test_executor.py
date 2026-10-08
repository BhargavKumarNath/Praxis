"""Retry executor: idempotent charges under duplicate / crashed / late / stale dispatch."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from praxis.dunning.executor import Charger, ExecStatus, RetryExecutor
from praxis.dunning.service import DunningService
from praxis.dunning.tasks import LocalTaskQueue
from praxis.payments.model import PaymentBehaviour
from praxis.payments.service import BillingService
from praxis.payments.synthetic import SyntheticPaymentGateway
from tests.dunning.helpers import POLICY, T0, Clock, failed, make_service, rows, succeeded
from tests.payments.helpers import PLAN, profile

pytestmark = pytest.mark.integration


class Setup:
    def __init__(
        self, engine: Engine, behaviour: PaymentBehaviour = PaymentBehaviour.CHARGE_FAILS
    ) -> None:
        self.gateway = SyntheticPaymentGateway(start=T0)
        published: list[object] = []
        billing = BillingService(
            self.gateway, type("P", (), {"publish": lambda s, e: published.append(e)})()
        )
        enrolment = billing.enrol(profile("cust_d8"), PLAN, behaviour)
        self.customer = enrolment.provider_customer_id
        self.invoice = str(enrolment.subscription.latest_invoice_id)
        self.billing = billing
        self.queue = LocalTaskQueue()
        self.clock = Clock(T0)
        self.service: DunningService = make_service(engine, queue=self.queue, clock=self.clock)
        self.executor = RetryExecutor(
            engine,
            {"synthetic": Charger.for_gateway(self.gateway)},
            POLICY,
            self.service,
            clock=self.clock,
        )
        self.engine = engine

    def charges(self) -> int:
        return len(self.gateway.fetch_invoice(self.invoice).charges)

    def open_case(self) -> str:
        self.service.handle(failed(self.invoice, 1, T0, amount=PLAN.base_fee_minor))
        return str(
            rows(self.engine, "SELECT job_id FROM retry_jobs WHERE state = 'scheduled'")[0][0]
        )

    def job_state(self, job_id: str) -> str:
        return str(
            rows(self.engine, "SELECT state FROM retry_jobs WHERE job_id = :j", j=job_id)[0][0]
        )


def test_declined_retry_is_recorded_and_duplicates_do_not_charge(pg_engine: Engine) -> None:
    s = Setup(pg_engine)
    job = s.open_case()
    s.clock.now = T0 + timedelta(days=3)
    assert s.charges() == 1
    result = s.executor.execute(job)
    assert result.status is ExecStatus.FAILED and result.detail == "card_declined"
    assert s.job_state(job) == "failed" and s.charges() == 2
    for _ in range(3):  # Cloud Tasks delivers at least once
        assert s.executor.execute(job).status is ExecStatus.STALE
    assert s.charges() == 2
    assert rows(
        pg_engine, "SELECT dispatches, outcome FROM retry_jobs WHERE job_id = :j", j=job
    ) == [(1, "declined")]


def test_crash_after_start_redispatch_reuses_the_idempotency_key(pg_engine: Engine) -> None:
    s = Setup(pg_engine)
    job = s.open_case()
    s.clock.now = T0 + timedelta(days=3)
    s.billing.update_payment_method(s.customer, PaymentBehaviour.SUCCEEDS, request_id="fix")
    guard = s.executor._guard(job)  # EXECUTING committed ...
    assert isinstance(guard, tuple) and s.job_state(job) == "executing"
    first = s.gateway.pay_invoice(s.invoice, idempotency_key=guard[0].idempotency_key)  # charged
    # ... the process dies before recording. The task is re-delivered:
    result = s.executor.execute(job)
    assert result.status is ExecStatus.SUCCEEDED and first.status.value == "succeeded"
    assert s.charges() == 2  # one retry charge, not two
    assert s.job_state(job) == "succeeded"


def test_already_paid_invoice_is_never_retried(pg_engine: Engine) -> None:
    s = Setup(pg_engine)
    job = s.open_case()
    s.billing.update_payment_method(s.customer, PaymentBehaviour.SUCCEEDS, request_id="fix")
    s.billing.retry_invoice(s.invoice, request_id="customer-portal")  # customer paid themselves
    s.clock.now = T0 + timedelta(days=3)
    result = s.executor.execute(job)
    assert result.status is ExecStatus.CANCELLED and result.detail == "already_paid"
    assert s.charges() == 2 and s.job_state(job) == "cancelled"


def test_recovered_case_cancels_without_a_provider_call(pg_engine: Engine) -> None:
    s = Setup(pg_engine)
    job = s.open_case()
    with pg_engine.begin() as c:  # a stale task survived its queue deletion
        c.execute(text("UPDATE retry_jobs SET state = 'scheduled' WHERE job_id = :j"), {"j": job})
    s.service.handle(succeeded(s.invoice, 2, T0 + timedelta(days=1)))
    assert s.executor.execute(job).status is ExecStatus.STALE  # cancelled by the service
    assert s.charges() == 1


def test_case_closed_guard(pg_engine: Engine) -> None:
    s = Setup(pg_engine)
    job = s.open_case()
    with pg_engine.begin() as c:
        c.execute(text("ALTER TABLE dunning_cases DISABLE TRIGGER dunning_cases_state_guard"))
        c.execute(text("UPDATE dunning_cases SET state = 'closed'"))
        c.execute(text("ALTER TABLE dunning_cases ENABLE TRIGGER dunning_cases_state_guard"))
    s.clock.now = T0 + timedelta(days=3)
    result = s.executor.execute(job)
    assert (result.status, result.detail) == (ExecStatus.CANCELLED, "case_closed")
    assert s.charges() == 1


def test_too_early_and_expired_dispatch(pg_engine: Engine) -> None:
    s = Setup(pg_engine)
    job = s.open_case()
    s.clock.now = T0 + timedelta(days=1)
    early = s.executor.execute(job)
    assert (
        early.status is ExecStatus.TOO_EARLY and not early.done and s.job_state(job) == "scheduled"
    )
    s.clock.now = T0 + timedelta(days=5)  # > run_at + 24 h
    late = s.executor.execute(job)
    assert late.status is ExecStatus.EXPIRED and s.job_state(job) == "expired"
    new = rows(pg_engine, "SELECT job_id, run_at FROM retry_jobs WHERE state = 'scheduled'")
    assert len(new) == 1 and new[0][0] != job  # re-planned from now
    assert s.charges() == 1


def test_unknown_job_and_missing_gateway(pg_engine: Engine) -> None:
    s = Setup(pg_engine)
    assert s.executor.execute("job-none").status is ExecStatus.STALE
    s.service.handle(failed("in_other", 1, T0, provider=None))
    job = rows(pg_engine, "SELECT job_id FROM retry_jobs WHERE invoice_id = 'in_other'")[0][0]
    s.clock.now = T0 + timedelta(days=3)
    result = s.executor.execute(job)
    assert (result.status, result.detail) == (ExecStatus.CANCELLED, "no_gateway")
    assert s.executor.handle(job) is True


def test_queue_drives_the_executor(pg_engine: Engine) -> None:
    s = Setup(pg_engine)
    job = s.open_case()
    s.clock.now = T0 + timedelta(days=3)
    assert s.queue.run_due(s.clock.now, s.executor.handle) == 1
    assert s.job_state(job) == "failed"
    name = next(iter(s.queue.history))
    assert s.queue.redeliver(name, s.executor.handle) is True  # duplicate delivery: no-op
    assert s.charges() == 2


def test_provider_state_and_gateway_errors(pg_engine: Engine) -> None:
    from dataclasses import replace

    from praxis.dunning.executor import _provider_reason
    from praxis.dunning.store import DunningRepo
    from praxis.payments.gateway import ForeignObject, GatewayError
    from praxis.payments.model import InvoiceStatus

    s = Setup(pg_engine)
    job_id = s.open_case()
    with pg_engine.connect() as conn:
        job = DunningRepo(conn).job(job_id)
    assert job is not None
    snap = s.gateway.fetch_invoice(s.invoice)
    assert _provider_reason(replace(snap, status=InvoiceStatus.VOID), job) == "invoice_closed"
    assert _provider_reason(replace(snap, charges=snap.charges * 2), job) == "attempt_already_made"
    assert _provider_reason(snap, job) is None

    class Refusing:
        provider = "synthetic"

        def fetch_invoice(self, invoice_id: str) -> object:
            raise ForeignObject("gone")

    def boom(invoice_id: str, key: str) -> object:
        raise GatewayError("card_error", "permanent")

    refusing = RetryExecutor(
        pg_engine,
        {"synthetic": Charger(boom, Refusing())},  # type: ignore[arg-type]
        POLICY,
        s.service,
        clock=s.clock,
    )
    s.clock.now = T0 + timedelta(days=3)
    result = refusing.execute(job_id)
    assert (result.status, result.detail) == (
        ExecStatus.CANCELLED,
        "provider_refused:foreign_object",
    )

    s.service.replan(s.invoice, trace_id="t")
    new_job = rows(pg_engine, "SELECT job_id, run_at FROM retry_jobs WHERE state = 'scheduled'")[0]
    s.clock.now = new_job[1]
    charging = RetryExecutor(
        pg_engine,
        {"synthetic": Charger(boom, s.gateway)},  # type: ignore[arg-type]
        POLICY,
        s.service,
        clock=s.clock,
    )
    failed_ = charging.execute(new_job[0])
    assert (failed_.status, failed_.detail) == (ExecStatus.FAILED, "card_error")
    assert s.job_state(new_job[0]) == "failed"


def test_executing_job_without_gateway_is_left_for_an_operator(pg_engine: Engine) -> None:
    s = Setup(pg_engine)
    job = s.open_case()
    with pg_engine.begin() as c:
        c.execute(text("UPDATE retry_jobs SET state = 'executing' WHERE job_id = :j"), {"j": job})
    bare = RetryExecutor(pg_engine, {}, POLICY, s.service)  # default wall clock
    assert bare.execute(job).detail == "no_gateway"
    assert s.job_state(job) == "executing" and bare.clock().tzinfo is not None
