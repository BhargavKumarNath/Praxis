"""Control-plane migrations and database-level guards (CHECKs, state-move trigger)."""

from __future__ import annotations

import importlib.util
from types import ModuleType

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, exc, inspect, text

from praxis.control.db import MIGRATIONS_DIR, alembic_config, migrate
from praxis.domain.dunning import DUNNING_TRANSITIONS, RETRY_JOB_TRANSITIONS
from praxis.domain.states import (
    CUSTOMER_TRANSITIONS,
    INVOICE_TRANSITIONS,
    SUBSCRIPTION_TRANSITIONS,
)

TABLES = {
    "allowed_state_moves",
    "processed_events",
    "entity_events",
    "customers",
    "subscriptions",
    "invoices",
    "payment_ledger",
    "state_transitions",
    "dead_letters",
    "dunning_cases",
    "dunning_decisions",
    "retry_jobs",
}


def _migration(name: str = "0001_control_plane") -> ModuleType:
    path = MIGRATIONS_DIR / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"m{name[:4]}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_creates_schema_and_is_rerunnable(empty_pg_url: str) -> None:
    migrate(empty_pg_url)
    migrate(empty_pg_url)  # no-op at head
    engine = create_engine(empty_pg_url)
    assert set(inspect(engine).get_table_names()) >= TABLES
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0004"
    engine.dispose()


def test_downgrade_then_upgrade_round_trips(empty_pg_url: str) -> None:
    migrate(empty_pg_url)
    command.downgrade(alembic_config(empty_pg_url), "base")
    engine = create_engine(empty_pg_url)
    assert TABLES.isdisjoint(inspect(engine).get_table_names())
    engine.dispose()
    migrate(empty_pg_url)


def test_offline_sql_can_be_rendered_for_review(capsys: pytest.CaptureFixture[str]) -> None:
    command.upgrade(alembic_config("postgresql+psycopg://u@h/db"), "head", sql=True)
    sql = capsys.readouterr().out
    assert "CREATE TABLE processed_events" in sql and "guard_state_move" in sql


def test_allowed_moves_equal_the_python_transition_closure() -> None:
    expected = {
        "customer": CUSTOMER_TRANSITIONS.reachable_pairs(),
        "subscription": SUBSCRIPTION_TRANSITIONS.reachable_pairs(),
        "invoice": INVOICE_TRANSITIONS.reachable_pairs(),
    }
    seeded = {**_migration().ALLOWED_MOVES, **_migration("0004_dunning").ALLOWED_MOVES}
    expected["dunning"] = DUNNING_TRANSITIONS.reachable_pairs()
    expected["retry_job"] = RETRY_JOB_TRANSITIONS.reachable_pairs()
    assert set(seeded) == set(expected)
    for machine, pairs in expected.items():
        assert set(seeded[machine]) == {(a.value, b.value) for a, b in pairs}, machine


def _customer(engine: Engine, state: str = "active") -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO customers (customer_id, state, region_id, industry, tier, "
                "is_existing, applied_count, pending_count, last_applied_at) "
                "VALUES ('c1', :s, 'r', 'i', 'starter', false, 1, 0, now())"
            ),
            {"s": state},
        )


@pytest.mark.parametrize(
    ("start", "target", "allowed"),
    [
        ("prospect", "active", True),
        ("active", "churned", True),
        ("churned", "active", False),
        ("lost", "active", False),
        ("active", "prospect", False),
    ],
)
def test_trigger_blocks_arbitrary_state_updates(
    pg_engine: Engine, start: str, target: str, allowed: bool
) -> None:
    _customer(pg_engine, start)
    stmt = text("UPDATE customers SET state = :t WHERE customer_id = 'c1'")
    if allowed:
        with pg_engine.begin() as conn:
            conn.execute(stmt, {"t": target})
        return
    with (
        pytest.raises(exc.IntegrityError, match="forbidden customer state move"),
        pg_engine.begin() as conn,
    ):
        conn.execute(stmt, {"t": target})


def test_unknown_state_value_violates_check(pg_engine: Engine) -> None:
    with pytest.raises(exc.IntegrityError):
        _customer(pg_engine, "zombie")


@pytest.mark.parametrize(
    ("amount", "paid", "state"),
    [(-1, 0, "open"), (100, 50, "open"), (100, 100, "open"), (100, 0, "paid")],
)
def test_invoice_money_invariants(pg_engine: Engine, amount: int, paid: int, state: str) -> None:
    with pytest.raises(exc.IntegrityError), pg_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invoices (invoice_id, customer_id, state, amount_minor, currency, "
                "period_start, period_end, attempts, amount_paid_minor, applied_count, "
                "pending_count, last_applied_at) VALUES ('i1', 'c1', :st, :a, 'GBP', "
                "'2026-01-01', '2026-01-31', 0, :p, 1, 0, now())"
            ),
            {"st": state, "a": amount, "p": paid},
        )


def test_ledger_cannot_record_an_invoice_twice(pg_engine: Engine) -> None:
    insert = text(
        "INSERT INTO payment_ledger (invoice_id, customer_id, amount_minor, currency, event_id, "
        "paid_at) VALUES ('i1', 'c1', 100, 'GBP', :e, now())"
    )
    with pg_engine.begin() as conn:
        conn.execute(insert, {"e": "00000000-0000-4000-8000-000000000001"})
    with pytest.raises(exc.IntegrityError), pg_engine.begin() as conn:
        conn.execute(insert, {"e": "00000000-0000-4000-8000-000000000002"})
