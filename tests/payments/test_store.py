"""Postgres webhook inbox and provider-ref store (migration 0003)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, exc, text

from praxis.errors import TransientError
from praxis.payments.model import Notification, NotificationKind
from praxis.payments.store import (
    MemoryRefStore,
    PostgresInbox,
    PostgresRefStore,
    RefConflict,
    RefStore,
)
from tests.payments.helpers import T0

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
LEASE = timedelta(seconds=60)


def note(
    event_id: str, object_id: str = "in_1", kind: NotificationKind = NotificationKind.INVOICE
) -> Notification:
    return Notification(
        "stripe", event_id, "invoice.paid", kind, object_id, T0, False, "2026-09-30.endive"
    )


def record(inbox: PostgresInbox, n: Notification) -> bool:
    return inbox.record(n, body_sha256="a" * 64, trace_id="b" * 32, correlation_id="c")


@pytest.fixture
def inbox(pg_engine: Engine) -> PostgresInbox:
    return PostgresInbox(pg_engine)


def test_record_is_idempotent_and_counts_deliveries(inbox: PostgresInbox) -> None:
    assert record(inbox, note("evt_1")) is True
    assert record(inbox, note("evt_1")) is False
    assert record(inbox, note("evt_1")) is False
    assert inbox.deliveries("stripe", "evt_1") == 3
    assert inbox.deliveries("stripe", "evt_missing") is None
    assert inbox.counts() == {"pending": 1}


def test_claim_leases_rows_and_does_not_hand_them_out_twice(inbox: PostgresInbox) -> None:
    for i in range(5):
        record(inbox, note(f"evt_{i}", object_id=f"in_{i}"))
    first = inbox.claim(limit=3, now=NOW, lease=LEASE)
    second = inbox.claim(limit=10, now=NOW, lease=LEASE)
    assert len(first) == 3 and len(second) == 2
    assert {i.event_id for i in first}.isdisjoint({i.event_id for i in second})
    assert inbox.claim(limit=10, now=NOW, lease=LEASE) == []  # all leased
    again = inbox.claim(limit=10, now=NOW + LEASE, lease=LEASE)  # lease expired: worker died
    assert len(again) == 5 and {i.attempts for i in again} == {2}
    item = again[0]
    assert (item.kind, item.trace_id, item.key[0]) == (NotificationKind.INVOICE, "b" * 32, "stripe")


def test_concurrent_claims_skip_locked_rows(pg_engine: Engine, inbox: PostgresInbox) -> None:
    for i in range(4):
        record(inbox, note(f"evt_{i}"))
    with pg_engine.connect() as conn, conn.begin():
        conn.execute(
            text("SELECT 1 FROM payment_webhook_inbox WHERE provider_event_id = 'evt_0' FOR UPDATE")
        )
        claimed = inbox.claim(limit=10, now=NOW, lease=LEASE)  # another worker holds evt_0
    assert sorted(i.event_id for i in claimed) == ["evt_1", "evt_2", "evt_3"]


def test_terminal_transitions(inbox: PostgresInbox) -> None:
    for i in range(4):
        record(inbox, note(f"evt_{i}"))
    keys = [("stripe", f"evt_{i}") for i in range(4)]
    assert inbox.complete([keys[0]], "events=3", NOW) == 1
    assert inbox.ignore([keys[1]], "foreign_object", NOW) == 1
    assert inbox.fail([keys[2]], "boom", NOW) == 1
    assert inbox.retry_later([keys[3]], "transient", NOW + timedelta(minutes=5)) == 1
    assert inbox.complete([keys[0]], "again", NOW) == 0  # terminal rows never change
    assert inbox.complete([], "nothing", NOW) == 0
    assert inbox.counts() == {"failed": 1, "ignored": 1, "pending": 1, "processed": 1}
    assert inbox.claim(limit=10, now=NOW, lease=LEASE) == []  # evt_3 waits for its backoff
    assert [
        i.event_id for i in inbox.claim(limit=10, now=NOW + timedelta(minutes=5), lease=LEASE)
    ] == ["evt_3"]
    assert inbox.requeue_failed(NOW) == 1
    requeued = inbox.claim(limit=10, now=NOW, lease=LEASE)
    assert [(i.event_id, i.attempts) for i in requeued] == [("evt_2", 1)]


def test_schema_rejects_inconsistent_rows(pg_engine: Engine, inbox: PostgresInbox) -> None:
    record(inbox, note("evt_1"))
    with pytest.raises(exc.IntegrityError), pg_engine.begin() as conn:
        conn.execute(
            text("UPDATE payment_webhook_inbox SET status = 'processed'")
        )  # no processed_at
    with pytest.raises(exc.IntegrityError), pg_engine.begin() as conn:
        conn.execute(text("UPDATE payment_webhook_inbox SET object_kind = 'charge'"))


def test_inbox_outage_is_transient() -> None:
    from praxis.control.db import make_engine

    dead = PostgresInbox(
        make_engine("postgresql+psycopg://praxis@127.0.0.1:1/none", connect_timeout_s=1)
    )
    with pytest.raises(TransientError):
        record(dead, note("evt_1"))


@pytest.fixture(params=["memory", "postgres"])
def refs(request: pytest.FixtureRequest, pg_engine: Engine) -> RefStore:
    return MemoryRefStore() if request.param == "memory" else PostgresRefStore(pg_engine)


def test_ref_store_semantics(refs: RefStore) -> None:
    assert refs.get("stripe", "customer", "cust_a") is None
    refs.put("stripe", "customer", "cust_a", "cus_1")
    refs.put("stripe", "customer", "cust_a", "cus_1")  # idempotent
    assert refs.get("stripe", "customer", "cust_a") == "cus_1"
    assert refs.praxis_key_for("stripe", "customer", "cus_1") == "cust_a"
    assert refs.praxis_key_for("stripe", "customer", "cus_2") is None
    with pytest.raises(RefConflict):
        refs.put("stripe", "customer", "cust_a", "cus_2")  # a key never moves
    with pytest.raises(RefConflict):
        refs.put("stripe", "customer", "cust_b", "cus_1")  # an object has one owner
    refs.put("synthetic", "customer", "cust_a", "cus_1")  # providers are separate namespaces
    assert refs.get("stripe", "customer", "cust_a") == "cus_1"


def test_refs_are_append_only_in_the_database(pg_engine: Engine) -> None:
    PostgresRefStore(pg_engine).put("stripe", "price", "praxis_growth_gbp_4900_month", "price_1")
    with pytest.raises(exc.IntegrityError), pg_engine.begin() as conn:
        conn.execute(text("UPDATE payment_provider_refs SET provider_id = 'price_2'"))
    with pytest.raises(exc.IntegrityError), pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM payment_provider_refs"))
