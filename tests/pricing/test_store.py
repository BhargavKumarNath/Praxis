"""Audit (required_test.md s12): no decision executes without a persisted decision record.

Both stores share one semantic test suite; the Postgres-only tests prove the database itself
refuses an execution or a mutation that bypasses the Python code.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import Engine, exc, text

from praxis.pricing.config import Mode
from praxis.pricing.decision import Decision, Status
from praxis.pricing.optimiser import optimise
from praxis.pricing.store import (
    DecisionStore,
    MemoryDecisionStore,
    NotExecutable,
    PostgresDecisionStore,
    RecordConflict,
    StoreError,
    last_execution_date,
)
from tests.pricing.helpers import open_policy, problem, single_segment

pytestmark = pytest.mark.integration


def change(mode: Mode = Mode.EXECUTE) -> Decision:
    pol = open_policy(max_step=0.05).with_mode(mode, allow_execute=True)
    d = optimise(single_segment(-0.5, 10_000.0, 100_000), pol)
    assert d.status is Status.CHANGE
    return d


def hold(mode: Mode = Mode.EXECUTE, as_of: date = date(2026, 7, 27)) -> Decision:
    pol = open_policy().with_mode(mode, allow_execute=True)
    d = optimise(single_segment(-2.0, 100_000.0, 200_000, as_of=as_of), pol)
    assert d.status is Status.HOLD
    return d


@pytest.fixture(params=["memory", "postgres"])
def decision_store(request: pytest.FixtureRequest) -> Iterator[DecisionStore]:
    if request.param == "memory":
        yield MemoryDecisionStore()
    else:
        yield PostgresDecisionStore(request.getfixturevalue("pg_engine"))


def test_record_is_idempotent_and_round_trips(decision_store: DecisionStore) -> None:
    d = change()
    assert decision_store.record(d) is True
    assert decision_store.record(d) is False  # retried cycle: no duplicate
    assert decision_store.get_record(d.decision_id) == d.record()


def test_a_cycle_cannot_be_decided_twice(decision_store: DecisionStore) -> None:
    d = change()
    decision_store.record(d)
    other = replace(d, lineage={**d.lineage, "forecast_model_version": "another"})
    assert other.decision_id != d.decision_id
    with pytest.raises(RecordConflict):
        decision_store.record(other)


def test_unrecorded_decisions_never_execute(decision_store: DecisionStore) -> None:
    with pytest.raises(NotExecutable, match="no audit record"):
        decision_store.execute(change().decision_id, "pricing-service")
    assert decision_store.executions() == []


def test_execute_mode_executes_from_the_record_once(decision_store: DecisionStore) -> None:
    d = change()
    decision_store.record(d)
    first = decision_store.execute(d.decision_id, "pricing-service")
    again = decision_store.execute(d.decision_id, "pricing-service")
    assert first == again  # idempotent
    assert first.to_price_micros == d.chosen_price_micros
    assert first.from_price_micros == d.current_price_micros
    assert [e.decision_id for e in decision_store.executions("api_requests")] == [d.decision_id]
    assert last_execution_date(decision_store, "api_requests") == first.executed_at.date()
    assert last_execution_date(decision_store, "gpu_minutes") is None


def test_shadow_decisions_never_execute(decision_store: DecisionStore) -> None:
    d = change(Mode.SHADOW)
    decision_store.record(d)
    with pytest.raises(NotExecutable, match="shadow"):
        decision_store.execute(d.decision_id, "pricing-service")
    with pytest.raises(NotExecutable):
        decision_store.approve(d.decision_id, "alice")


def test_recommendations_need_a_named_approval(decision_store: DecisionStore) -> None:
    d = change(Mode.RECOMMEND)
    decision_store.record(d)
    with pytest.raises(NotExecutable, match="approval"):
        decision_store.execute(d.decision_id, "pricing-service")
    with pytest.raises(StoreError):
        decision_store.approve(d.decision_id, "  ")
    decision_store.approve(d.decision_id, "pricing-lead")
    decision_store.approve(d.decision_id, "pricing-lead")  # idempotent
    assert decision_store.execute(d.decision_id, "pricing-service").to_price_micros > 0


def test_only_changes_execute(decision_store: DecisionStore) -> None:
    d = hold()
    decision_store.record(d)
    with pytest.raises(NotExecutable, match="not a change"):
        decision_store.execute(d.decision_id, "pricing-service")
    with pytest.raises(StoreError):
        decision_store.execute(d.decision_id, "")
    with pytest.raises(NotExecutable):
        decision_store.approve("dec-missing", "alice")


def test_unavailable_decisions_are_recorded_too(decision_store: DecisionStore) -> None:
    d = optimise(problem(current_price_micros=-1), open_policy())
    assert d.status is Status.UNAVAILABLE
    assert decision_store.record(d) is True
    rec = decision_store.get_record(d.decision_id)
    assert rec is not None and rec["errors"] and rec["current_price_micros"] is None


# ------------------------------------------------------------- database-level guards
def _insert_execution(engine: Engine, d: Decision, to_price: int | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO price_executions (decision_id, product, from_price_micros, "
                "to_price_micros, executed_by) VALUES (:d, :p, :f, :t, 'raw-sql')"
            ),
            {
                "d": d.decision_id,
                "p": d.product,
                "f": d.current_price_micros,
                "t": to_price or d.chosen_price_micros or 1,
            },
        )


def test_database_refuses_an_execution_without_a_record(pg_engine: Engine) -> None:
    with pytest.raises(exc.IntegrityError, match="no audit record"):
        _insert_execution(pg_engine, change())


@pytest.mark.parametrize("mode", [Mode.SHADOW, Mode.RECOMMEND])
def test_database_refuses_shadow_and_unapproved_executions(pg_engine: Engine, mode: Mode) -> None:
    d = change(mode)
    PostgresDecisionStore(pg_engine).record(d)
    with pytest.raises(exc.IntegrityError, match=r"shadow|approval"):
        _insert_execution(pg_engine, d)


def test_database_refuses_a_price_other_than_the_decided_one(pg_engine: Engine) -> None:
    d = change()
    PostgresDecisionStore(pg_engine).record(d)
    assert d.chosen_price_micros is not None
    with pytest.raises(exc.IntegrityError, match="does not match"):
        _insert_execution(pg_engine, d, to_price=d.chosen_price_micros + 100)
    h = hold()
    PostgresDecisionStore(pg_engine).record(h)
    with pytest.raises(exc.IntegrityError, match="does not match"):
        _insert_execution(pg_engine, h, to_price=123)


def test_database_refuses_approving_a_non_recommendation(pg_engine: Engine) -> None:
    d = change(Mode.EXECUTE)
    PostgresDecisionStore(pg_engine).record(d)
    with pytest.raises(exc.IntegrityError, match="cannot be approved"), pg_engine.begin() as c:
        c.execute(
            text("INSERT INTO pricing_approvals (decision_id, approved_by) VALUES (:d, 'x')"),
            {"d": d.decision_id},
        )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE pricing_decisions SET chosen_price_micros = 1",
        "DELETE FROM pricing_decisions",
        "UPDATE price_executions SET to_price_micros = 1",
        "DELETE FROM price_executions",
        "DELETE FROM pricing_approvals",
    ],
)
def test_audit_tables_are_append_only(pg_engine: Engine, sql: str) -> None:
    store = PostgresDecisionStore(pg_engine)
    d = change(Mode.RECOMMEND)
    store.record(d)
    store.approve(d.decision_id, "pricing-lead")
    store.execute(d.decision_id, "pricing-service")
    with pytest.raises(exc.IntegrityError, match="append-only"), pg_engine.begin() as conn:
        conn.execute(text(sql))


def test_tampered_records_cannot_execute(pg_engine: Engine) -> None:
    d = change()
    store = PostgresDecisionStore(pg_engine)
    store.record(d)
    with pg_engine.begin() as conn:  # bypass the append-only trigger as a superuser would
        conn.execute(text("ALTER TABLE pricing_decisions DISABLE TRIGGER USER"))
        conn.execute(
            text(
                "UPDATE pricing_decisions SET record = jsonb_set(record::jsonb, "
                "'{chosen_price_micros}', '1')::json WHERE decision_id = :d"
            ),
            {"d": d.decision_id},
        )
        conn.execute(text("ALTER TABLE pricing_decisions ENABLE TRIGGER USER"))
    with pytest.raises(NotExecutable, match="checksum"):
        store.execute(d.decision_id, "pricing-service")
