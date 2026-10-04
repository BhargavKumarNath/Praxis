"""Control-plane store against real Postgres: idempotency, ordering, money, failures."""

from __future__ import annotations

import random
import threading
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import Engine, event, text

from praxis.control.db import is_transient, make_engine
from praxis.control.store import (
    MAX_STORED_DEAD_LETTER_BYTES,
    ApplyStatus,
    ControlPlaneStore,
    DeadLetterRecord,
    EventRecord,
    snapshot_checksum,
)
from praxis.domain.projections import fold_customer
from praxis.streaming.faults import DatabaseOutage
from praxis.streaming.transport import TransientError
from tests.streaming.helpers import customer_lifecycle, invoice_lifecycle, logged

OPS = "operational"


def rec(e: dict[str, Any], attempt: int = 1) -> EventRecord:
    return EventRecord(
        event_id=e["event_id"],
        event_type=e["event_type"],
        entity_id=e["entity_id"],
        occurred_at=datetime.fromisoformat(e["occurred_at"]),
        payload=e["payload"],
        trace_id=e["trace_id"],
        correlation_id=e["correlation_id"],
        causation_id=e["causation_id"],
        delivery_attempt=attempt,
    )


def _scalar(engine: Engine, sql: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar_one()


def test_lifecycle_applies_and_writes_projection(store: ControlPlaneStore) -> None:
    events = customer_lifecycle(changes=2) + invoice_lifecycle(fail_first=1)
    assert [store.apply_event(OPS, rec(e)).status for e in events] == [ApplyStatus.APPLIED] * len(
        events
    )
    snap = store.snapshot()
    assert snap["customers"]["cust_a"][:2] == ["churned", "enterprise"]
    assert snap["customers"]["cust_a"][5:] == ["cancelled", ["gpu-inference"], 9_901]
    assert snap["invoices"]["inv_1"] == ["cust_a", "paid", 12_345, 2, 12_345, 0]
    assert (snap["ledger_entries"], snap["ledger_total_minor"]) == (1, 12_345)
    expected = fold_customer("cust_a", [logged(e) for e in customer_lifecycle(changes=2)])
    with store.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT machine, from_state, to_state, event_id::text FROM state_transitions "
                "WHERE aggregate_key = 'cust_a'"
            )
        ).all()
    assert {tuple(r) for r in rows} == {
        (t.machine.value, t.from_state, t.to_state, t.event_id) for t in expected.transitions
    }


def test_duplicate_is_a_no_op(store: ControlPlaneStore) -> None:
    events = invoice_lifecycle()
    for e in events:
        store.apply_event(OPS, rec(e))
    before = store.snapshot()
    version = _scalar(store.engine, "SELECT version FROM invoices")
    for e in events * 5:
        assert store.apply_event(OPS, rec(e, attempt=2)).status is ApplyStatus.DUPLICATE
    assert store.snapshot() == before
    assert _scalar(store.engine, "SELECT version FROM invoices") == version
    assert _scalar(store.engine, "SELECT count(*) FROM processed_events") == len(events)


def test_repeated_success_never_collects_twice(store: ControlPlaneStore) -> None:
    events = invoice_lifecycle()
    success = events[-1]
    for e in [success, *events, success, success]:
        store.apply_event(OPS, rec(e))
    assert _scalar(store.engine, "SELECT count(*) FROM payment_ledger") == 1
    assert _scalar(store.engine, "SELECT sum(amount_minor) FROM payment_ledger") == 12_345


def test_out_of_order_pair_is_buffered_then_applied(store: ControlPlaneStore) -> None:
    created, attempted, succeeded = invoice_lifecycle()
    assert store.apply_event(OPS, rec(succeeded)).pending == 1
    assert store.pending_summary()["invoice_orphans"] == 1  # no invoice row yet
    assert _scalar(store.engine, "SELECT count(*) FROM payment_ledger") == 0
    assert store.apply_event(OPS, rec(created)).pending == 1  # success still waits for attempt
    assert store.snapshot()["invoices"]["inv_1"][1] == "open"
    assert store.apply_event(OPS, rec(attempted)).pending == 0
    assert store.snapshot()["invoices"]["inv_1"][1] == "paid"
    assert _scalar(store.engine, "SELECT count(*) FROM payment_ledger") == 1
    assert store.pending_summary() == dict.fromkeys(
        ("customer_pending", "invoice_pending", "customer_orphans", "invoice_orphans"), 0
    )


def test_non_stateful_event_writes_nothing(store: ControlPlaneStore) -> None:
    usage = {
        **customer_lifecycle()[0],
        "event_type": "usage.observed",
        "payload": {
            "product": "x",
            "region_id": "r",
            "units": 1,
            "throttled_units": 0,
            "unit_price_micros": 1,
        },
    }
    assert store.apply_event(OPS, rec(usage)).status is ApplyStatus.IGNORED
    assert _scalar(store.engine, "SELECT count(*) FROM processed_events") == 0


def test_independent_consumers_have_independent_idempotency(store: ControlPlaneStore) -> None:
    e = customer_lifecycle()[0]
    assert store.apply_event("a", rec(e)).status is ApplyStatus.APPLIED
    assert store.apply_event("b", rec(e)).status is ApplyStatus.APPLIED
    assert store.apply_event("a", rec(e)).status is ApplyStatus.DUPLICATE
    assert _scalar(store.engine, "SELECT count(*) FROM entity_events") == 1


def test_correlation_lookup(store: ControlPlaneStore) -> None:
    events = invoice_lifecycle()
    for e in events:
        store.apply_event(OPS, rec(e))
    rows = store.processed_for_correlation(events[0]["correlation_id"])
    assert {r["event_id"] for r in rows} == {e["event_id"] for e in events}
    assert {r["trace_id"] for r in rows} == {events[0]["trace_id"]}


def test_injected_outage_is_transient_and_leaves_no_partial_write(
    store: ControlPlaneStore,
) -> None:
    outage = DatabaseOutage(store.engine)
    e = customer_lifecycle()[0]
    outage.fail_next(1)
    with pytest.raises(TransientError):
        store.apply_event(OPS, rec(e))
    # Let the idempotency insert succeed, fail the next statement: the whole txn rolls back.
    outage.fail_next(1, after=1)
    with pytest.raises(TransientError):
        store.apply_event(OPS, rec(e))
    assert outage.injected == 2
    outage.remove()
    assert _scalar(store.engine, "SELECT count(*) FROM processed_events") == 0
    assert store.apply_event(OPS, rec(e)).status is ApplyStatus.APPLIED
    assert store.apply_event(OPS, rec(e)).status is ApplyStatus.DUPLICATE


def test_connection_killed_mid_transaction_is_transient(pg_url: str) -> None:
    """A real server-side failure: the backend is terminated after the idempotency insert."""
    engine = make_engine(pg_url)
    store = ControlPlaneStore(engine)
    admin = make_engine(pg_url)
    armed = {"kill": True}

    def kill_after_mark(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if armed["kill"] and statement.startswith("INSERT INTO processed_events"):
            armed["kill"] = False
            pid = conn.connection.dbapi_connection.info.backend_pid
            with admin.begin() as other:
                other.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})

    event.listen(engine, "after_cursor_execute", kill_after_mark)
    e = customer_lifecycle()[0]
    with pytest.raises(TransientError):
        store.apply_event(OPS, rec(e))
    assert _scalar(admin, "SELECT count(*) FROM processed_events") == 0  # rolled back
    assert store.apply_event(OPS, rec(e)).status is ApplyStatus.APPLIED  # pool recovers
    engine.dispose()
    admin.dispose()


def test_non_transient_database_errors_propagate(store: ControlPlaneStore) -> None:
    from sqlalchemy import exc

    assert not is_transient(exc.IntegrityError("x", None, Exception("dup")))
    assert is_transient(exc.OperationalError("x", None, Exception("gone")))
    huge = invoice_lifecycle(amount=2**63)[0]  # BIGINT overflow: a poison message
    with pytest.raises(exc.DataError):
        store.apply_event(OPS, rec(huge))


def test_concurrent_consumers_converge(pg_url: str) -> None:
    """Four workers apply a shuffled, duplicated stream for the same aggregates."""
    engine = make_engine(pg_url, pool_size=8)
    store = ControlPlaneStore(engine)
    events = [
        *customer_lifecycle("cust_a", changes=3),
        *customer_lifecycle("cust_b", new=False, changes=1, churn=False),
        *invoice_lifecycle("cust_a", "inv_1", fail_first=2),
        *invoice_lifecycle("cust_b", "inv_2", fail_first=3),
    ]
    work = events * 3
    random.Random(7).shuffle(work)  # noqa: S311 - deterministic test ordering
    chunks = [work[i::4] for i in range(4)]
    errors: list[BaseException] = []

    def run(chunk: list[dict[str, Any]]) -> None:
        try:
            for e in chunk:
                store.apply_event(OPS, rec(e))
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(c,)) for c in chunks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    snap = store.snapshot()
    assert snap["customers"]["cust_a"][0] == "churned"
    assert snap["customers"]["cust_b"][:2] == ["active", "growth"]
    assert snap["invoices"]["inv_1"][1:5] == ["paid", 12_345, 3, 12_345]
    assert snap["invoices"]["inv_2"][1:5] == ["uncollectible", 12_345, 3, 0]
    assert snap["ledger_entries"] == 1
    assert _scalar(engine, "SELECT count(*) FROM processed_events") == len(events)
    engine.dispose()


def test_dead_letters_dedupe_and_count(store: ControlPlaneStore) -> None:
    def dl(
        data: bytes, event_id: str | None = None, reason: str = "malformed_json"
    ) -> DeadLetterRecord:
        return DeadLetterRecord("sub", reason, None, event_id, None, None, None, 1, data)

    assert store.record_dead_letter(dl(b"x")) is True
    assert store.record_dead_letter(dl(b"x")) is False
    assert store.record_dead_letter(dl(b"y", "e1", "max_delivery_attempts_exceeded")) is True
    assert (
        store.record_dead_letter(dl(b"other-bytes", "e1", "max_delivery_attempts_exceeded"))
        is False
    )
    assert store.record_dead_letter(dl(b"z" * (MAX_STORED_DEAD_LETTER_BYTES + 1))) is True
    assert store.dead_letter_counts() == {"malformed_json": 2, "max_delivery_attempts_exceeded": 1}
    assert _scalar(store.engine, "SELECT count(*) FROM dead_letters WHERE data IS NULL") == 1


def test_snapshot_checksum_is_stable(store: ControlPlaneStore) -> None:
    for e in customer_lifecycle():
        store.apply_event(OPS, rec(e))
    assert snapshot_checksum(store.snapshot()) == snapshot_checksum(store.snapshot())
