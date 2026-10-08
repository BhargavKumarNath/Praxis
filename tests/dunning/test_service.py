"""Dunning service on Postgres: transitions, decisions, scheduling, duplicates, ordering."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Engine, exc, text

from praxis.control.store import ControlPlaneStore
from praxis.domain.dunning import DunningState, RetryJobState
from praxis.dunning.service import DunningEvent, retry_key, task_name
from praxis.dunning.store import DunningRepo
from praxis.dunning.tasks import EnqueueStatus, LocalTaskQueue, RetryTask
from praxis.recovery.artifact import RecoveryArtifact, load_artifact
from tests.dunning.helpers import (
    CUST,
    T0,
    Clock,
    case_state,
    churned,
    failed,
    make_service,
    rows,
    seed_history,
    succeeded,
)
from tests.recovery.conftest import build_artifact

pytestmark = pytest.mark.integration
INV = "syn_in_000001"


def jobs(engine: Engine, invoice: str = INV) -> list[tuple[str, int, str]]:
    return [
        (r[0], r[1], r[2])
        for r in rows(
            engine,
            "SELECT state, attempt_number, task_name FROM retry_jobs WHERE invoice_id = :i "
            "ORDER BY created_at, job_id",
            i=invoice,
        )
    ]


def test_first_failure_opens_grace_records_decision_and_schedules(pg_engine: Engine) -> None:
    queue = LocalTaskQueue()
    service = make_service(pg_engine, queue=queue)
    out = service.handle(failed(INV, 1, T0))
    assert out.status == "applied" and out.state is DunningState.GRACE
    assert case_state(pg_engine, INV) == "grace"
    ((state, attempt, name),) = jobs(pg_engine)
    assert (state, attempt) == ("scheduled", 2)
    run_at = rows(pg_engine, "SELECT run_at, idempotency_key, enqueued_at FROM retry_jobs")[0]
    assert run_at[0] == T0 + timedelta(days=3) and run_at[1] == retry_key(INV, 2)
    assert run_at[2] is not None  # outbox flushed after commit
    assert [t.task.run_at for t in queue.tasks.values() if t.task.name == name] == [run_at[0]]
    decision = rows(
        pg_engine,
        "SELECT policy_kind, fallback_reason, action, stage, features FROM dunning_decisions",
    )
    assert decision == [("baseline", "model_unavailable", "retry", "grace", None)]
    audit = rows(
        pg_engine,
        "SELECT from_state, to_state FROM state_transitions "
        "WHERE machine = 'dunning' ORDER BY recorded_at",
    )
    assert audit == [(None, "past_due"), ("past_due", "grace")]


def test_duplicate_delivery_has_no_second_effect(pg_engine: Engine) -> None:
    queue = LocalTaskQueue()
    service = make_service(pg_engine, queue=queue)
    event = failed(INV, 1, T0)
    assert service.handle(event).status == "applied"
    assert service.handle(event).status == "duplicate"
    assert len(jobs(pg_engine)) == 1 and len(queue.tasks) == 1
    assert len(rows(pg_engine, "SELECT 1 FROM dunning_decisions")) == 1


def test_attempts_follow_the_baseline_to_suspension(pg_engine: Engine) -> None:
    queue = LocalTaskQueue()
    service = make_service(pg_engine, queue=queue)
    service.handle(failed(INV, 1, T0))
    second = service.handle(failed(INV, 2, T0 + timedelta(days=3)))
    assert second.state is DunningState.RESTRICTED
    third = service.handle(failed(INV, 3, T0 + timedelta(days=10)))
    assert third.state is DunningState.SUSPENDED and third.decision is not None
    assert third.decision.action == "stop"
    states = jobs(pg_engine)
    assert [(s, a) for s, a, _ in states] == [("superseded", 2), ("superseded", 3)]
    assert not queue.tasks  # superseded tasks were deleted from the queue
    late = rows(pg_engine, "SELECT run_at FROM retry_jobs WHERE attempt_number = 3")[0][0]
    assert late == T0 + timedelta(days=10)  # 3 + 7 days after the first failure


def test_recovery_cancels_scheduled_retries(pg_engine: Engine) -> None:
    queue = LocalTaskQueue()
    service = make_service(pg_engine, queue=queue)
    service.handle(failed(INV, 1, T0))
    out = service.handle(succeeded(INV, 2, T0 + timedelta(days=1)))
    assert out.state is DunningState.RECOVERED and case_state(pg_engine, INV) == "recovered"
    assert [s for s, _, _ in jobs(pg_engine)] == ["cancelled"] and not queue.tasks
    assert service.handle(failed(INV, 3, T0 + timedelta(days=2))).status == "stale"
    assert rows(pg_engine, "SELECT recovered_at FROM dunning_cases")[0][0] == T0 + timedelta(days=1)


def test_out_of_order_success_first(pg_engine: Engine) -> None:
    service = make_service(pg_engine)
    assert service.handle(succeeded(INV, 2, T0 + timedelta(days=3))).state is DunningState.RECOVERED
    assert service.handle(failed(INV, 1, T0)).status == "stale"
    assert jobs(pg_engine) == [] and case_state(pg_engine, INV) == "recovered"
    assert service.handle(succeeded(INV, 1, T0)).status == "ignored"  # never in dunning
    assert service.handle(succeeded(INV, 3, T0 + timedelta(days=4))).status == "stale"


def test_late_first_failure_moves_time_zero_back(pg_engine: Engine) -> None:
    service = make_service(pg_engine)
    service.handle(failed(INV, 2, T0 + timedelta(days=3)))  # attempt 2 arrives first
    assert service.handle(failed(INV, 1, T0)).status == "stale"
    opened, reason = rows(pg_engine, "SELECT opened_at, first_reason FROM dunning_cases")[0]
    assert opened == T0 and reason == "insufficient_funds"


def test_churn_closes_open_cases(pg_engine: Engine) -> None:
    queue = LocalTaskQueue()
    service = make_service(pg_engine, queue=queue)
    service.handle(failed(INV, 1, T0))
    service.handle(failed("syn_in_000002", 1, T0))
    service.handle(succeeded("syn_in_000002", 2, T0 + timedelta(days=3)))
    service.handle(churned(T0 + timedelta(days=4)))
    assert (
        case_state(pg_engine, INV) == "closed"
        and case_state(pg_engine, "syn_in_000002") == "recovered"
    )
    assert not queue.tasks
    assert rows(
        pg_engine, "SELECT closed_reason FROM dunning_cases WHERE invoice_id = :i", i=INV
    ) == [("voluntary",)]


def test_irrelevant_events_are_ignored(pg_engine: Engine) -> None:
    ev = DunningEvent(
        "6f1c2c1e-6c1b-4a53-9d0e-2a1f6b7c8d90", "usage.observed", CUST, T0, {}, "t", "c"
    )
    assert make_service(pg_engine).handle(ev).status == "ignored"


class BrokenQueue(LocalTaskQueue):
    fail = True

    def enqueue(self, task: RetryTask) -> EnqueueStatus:
        if self.fail:
            raise ConnectionError("queue down")
        return super().enqueue(task)

    def cancel(self, name: str) -> bool:
        raise ConnectionError("queue down")


def test_outbox_survives_queue_outages(pg_engine: Engine) -> None:
    queue = BrokenQueue()
    service = make_service(pg_engine, queue=queue)
    service.handle(failed(INV, 1, T0))
    assert rows(pg_engine, "SELECT enqueued_at FROM retry_jobs") == [(None,)]
    assert not queue.tasks
    queue.fail = False
    assert service.flush_outbox() == 1 and len(queue.tasks) == 1
    assert service.flush_outbox() == 0
    # a cancel that fails is logged; the job is cancelled in Postgres regardless
    service.handle(succeeded(INV, 2, T0 + timedelta(days=1)))
    assert [s for s, _, _ in jobs(pg_engine)] == ["cancelled"]


def test_database_forbids_duplicate_live_jobs_and_charges(pg_engine: Engine) -> None:
    service = make_service(pg_engine)
    service.handle(failed(INV, 1, T0))
    with pg_engine.connect() as conn:
        job = DunningRepo(conn).live_jobs(INV)[0]
    clone = {
        "j": "job-x",
        "i": INV,
        "d": job.decision_id,
        "n": 2,
        "r": T0,
        "t": "retry-x",
        "k": "k",
    }
    insert = text(
        "INSERT INTO retry_jobs (job_id, invoice_id, decision_id, attempt_number, "
        "run_at, task_name, idempotency_key, state) VALUES (:j, :i, :d, :n, :r, :t, :k, :s)"
    )
    with pytest.raises(exc.IntegrityError, match="one_live_per_invoice"), pg_engine.begin() as c:
        c.execute(insert, {**clone, "s": "scheduled"})
    with pg_engine.begin() as c:  # a second charge for the same attempt is refused too
        c.execute(text("UPDATE retry_jobs SET state = 'executing'"))
        c.execute(text("UPDATE retry_jobs SET state = 'failed'"))
    with pytest.raises(exc.IntegrityError, match="one_charge_per_attempt"), pg_engine.begin() as c:
        c.execute(insert, {**clone, "s": "succeeded"})
    with pytest.raises(exc.IntegrityError, match="forbidden"), pg_engine.begin() as c:
        c.execute(text("UPDATE retry_jobs SET state = 'scheduled'"))
    with pytest.raises(exc.IntegrityError, match="forbidden dunning"), pg_engine.begin() as c:
        c.execute(text("UPDATE dunning_cases SET state = 'past_due'"))
    with pytest.raises(exc.IntegrityError), pg_engine.begin() as c:  # decisions are append-only
        c.execute(text("UPDATE dunning_decisions SET action = 'stop'"))
    assert task_name(INV, 2, "d") != task_name(INV, 2, "e")


@pytest.fixture(scope="module")
def artifact(tmp_path_factory: pytest.TempPathFactory) -> RecoveryArtifact:
    return load_artifact(build_artifact(tmp_path_factory.mktemp("dunning-models"), created=T0))


def test_model_policy_uses_control_plane_features(
    pg_engine: Engine, store: ControlPlaneStore, artifact: RecoveryArtifact
) -> None:
    seed_history(store, INV)
    service = make_service(pg_engine, artifact=artifact, clock=Clock())
    out = service.handle(failed(INV, 1, T0))
    assert out.decision is not None and out.decision.fallback_reason is None
    kind, model, feats = rows(
        pg_engine, "SELECT policy_kind, model_version, features FROM dunning_decisions"
    )[0]
    assert kind == "model" and model == artifact.model_version
    assert feats["tier"] == "growth" and feats["reason"] == "insufficient_funds"
    assert feats["payment_method"] == "card" and feats["tenure_days"] == 340
    # without the invoice in the control plane the model cannot be fed: baseline, recorded
    other = service.handle(failed("syn_in_000009", 1, T0, customer="cust_unknown"))
    assert other.decision is not None and other.decision.fallback_reason == "features_unavailable"


def test_replan_after_expiry(pg_engine: Engine) -> None:
    clock = Clock(T0 + timedelta(days=5))
    service = make_service(pg_engine, clock=clock)
    service.handle(failed(INV, 1, T0))
    out = service.replan(INV, trace_id="t")
    assert out.status == "applied"
    assert [s for s, _, _ in jobs(pg_engine)] == ["superseded", "scheduled"]
    assert service.replan("nope", trace_id="t").status == "stale"
    with pg_engine.begin() as c:
        c.execute(text("UPDATE retry_jobs SET state = 'cancelled' WHERE state = 'scheduled'"))
    service.handle(succeeded(INV, 2, T0 + timedelta(days=6)))
    assert service.replan(INV, trace_id="t").status == "stale"
    assert RetryJobState.SCHEDULED.value not in [s for s, _, _ in jobs(pg_engine)]
