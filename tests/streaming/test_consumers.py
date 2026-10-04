"""Consumer edge branches: dead-letter parsing, transient paths, LRU, warehouse errors."""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pytest

from praxis.control.store import ControlPlaneStore, DeadLetterRecord
from praxis.data.warehouse import Warehouse
from praxis.events.codec import decode, encode
from praxis.streaming.consumers.dead_letter import (
    MAX_DELIVERY_ATTEMPTS_EXCEEDED,
    DeadLetterWorker,
    to_dead_letter,
)
from praxis.streaming.consumers.monitoring import MonitoringConsumer
from praxis.streaming.consumers.warehouse import WarehouseConsumer
from praxis.streaming.faults import DatabaseOutage
from praxis.streaming.metrics import StreamMetrics
from praxis.streaming.runtime import DLQ_ATTEMPT, DLQ_REASON, DLQ_SUBSCRIPTION
from praxis.streaming.transport import (
    DEAD_LETTER_SOURCE_DELIVERY_COUNT,
    DEAD_LETTER_SOURCE_SUBSCRIPTION,
    Delivery,
    Disposition,
    TransientError,
)
from tests.streaming.helpers import customer_lifecycle

EVENT = customer_lifecycle()[0]


def _d(attrs: dict[str, str], data: bytes = b"x", sub: str = "dlq-sub") -> Delivery:
    return Delivery(sub, "m", "a", data, attrs, datetime.now(UTC))


def test_runtime_routed_dead_letter_attributes() -> None:
    _, attrs = encode(EVENT)
    rec = to_dead_letter(
        _d({**attrs, DLQ_REASON: "invalid_payload", DLQ_SUBSCRIPTION: "ops", DLQ_ATTEMPT: "1"})
    )
    assert (rec.reason, rec.source_subscription, rec.delivery_attempt) == (
        "invalid_payload",
        "ops",
        1,
    )
    assert (rec.trace_id, rec.correlation_id) == (EVENT["trace_id"], EVENT["correlation_id"])
    assert rec.event_id == EVENT["event_id"]


def test_broker_forwarded_dead_letter_attributes() -> None:
    rec = to_dead_letter(
        _d({DEAD_LETTER_SOURCE_SUBSCRIPTION: "wh", DEAD_LETTER_SOURCE_DELIVERY_COUNT: "5"})
    )
    assert (rec.reason, rec.source_subscription, rec.delivery_attempt) == (
        MAX_DELIVERY_ATTEMPTS_EXCEEDED, "wh", 5,
    )  # fmt: skip
    assert rec.event_id is None and rec.trace_id is None


def test_garbage_attributes_are_not_trusted() -> None:
    rec = to_dead_letter(
        _d({"trace_id": "nope", "correlation_id": "nope", DLQ_ATTEMPT: "many", "event_id": ""})
    )
    assert (rec.reason, rec.source_subscription) == ("unknown", "unknown")
    assert (rec.trace_id, rec.correlation_id, rec.delivery_attempt, rec.event_id) == (None,) * 4


def test_dead_letter_worker_nacks_when_database_is_down(store: ControlPlaneStore) -> None:
    metrics = StreamMetrics()
    worker = DeadLetterWorker(store, metrics)
    outage = DatabaseOutage(store.engine)
    outage.fail_next(1)
    assert worker.process([_d({DLQ_REASON: "malformed_json"})]) == [Disposition.NACK]
    assert metrics.count("dlq-sub", "nack_transient") == 1
    assert worker.process([_d({DLQ_REASON: "malformed_json"})]) == [Disposition.ACK]
    assert worker.process([_d({DLQ_REASON: "malformed_json"})]) == [Disposition.ACK]
    assert metrics.count("dlq-sub", "dead_letters_recorded") == 1
    assert metrics.count("dlq-sub", "dead_letters_duplicate") == 1


def test_dead_letter_breakdown(store: ControlPlaneStore) -> None:
    for sub, reason, data in [("a", "r1", b"1"), ("a", "r1", b"2"), ("b", "r2", b"3")]:
        store.record_dead_letter(
            DeadLetterRecord(sub, reason, None, None, None, None, None, 1, data)
        )
    assert store.dead_letter_breakdown() == {"a": {"r1": 2}, "b": {"r2": 1}}


def test_monitoring_lru_is_bounded() -> None:
    metrics = StreamMetrics()
    consumer = MonitoringConsumer(metrics, dedupe_capacity=2)
    events = [decode(*encode(e)) for e in customer_lifecycle()[:3]]
    for e in events:
        consumer.handle(e, _d({}, sub="mon"))
    assert consumer.handle(events[0], _d({}, sub="mon")) == "observed"  # evicted: not detected
    assert consumer.handle(events[2], _d({}, sub="mon")) == "duplicate"
    assert metrics.count("mon", "unique_events_observed") == 4


def test_warehouse_io_errors_are_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    with Warehouse(None) as w:
        w.migrate()
        consumer = WarehouseConsumer(w)

        def locked(*_: object) -> int:
            raise duckdb.IOException("Could not set lock on file")

        monkeypatch.setattr(w, "insert_events", locked)
        with pytest.raises(TransientError):
            consumer.handle(decode(*encode(EVENT)), _d({}))


def test_streaming_insert_does_not_scan_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: counting the table per batch made 10K-customer streaming quadratic."""
    with Warehouse(None) as w:
        w.migrate()

        def forbidden(*_: object) -> int:
            raise AssertionError("insert_events must not count the table")

        monkeypatch.setattr(w, "count", forbidden)
        events = customer_lifecycle()
        assert w.insert_events(events, "s") == len(events)
        assert w.insert_events(events[:2] + customer_lifecycle("cust_b")[:1], "s") == 1
