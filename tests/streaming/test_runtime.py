"""Consumer runtime: failure classification, DLQ routing, tracing, batching."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest

from praxis.errors import PermanentError, TransientError
from praxis.events.codec import ATTR_SENT_AT, DecodedEvent, encode
from praxis.observability import LatencySample
from praxis.streaming.memory import MemoryBroker
from praxis.streaming.metrics import StreamMetrics
from praxis.streaming.runtime import (
    DLQ_ATTEMPT,
    DLQ_REASON,
    DLQ_SUBSCRIPTION,
    ConsumerWorker,
    DeadLetterRouter,
    transport_latency_ms,
)
from praxis.streaming.topology import DLQ_INSPECT, build_topology
from praxis.streaming.transport import Delivery, Disposition
from praxis.tracing import current_correlation_id, current_trace_id
from tests.streaming.helpers import customer_lifecycle

TOPO = build_topology()
DLQ = TOPO.by_role(DLQ_INSPECT).name
EVENTS = customer_lifecycle()


def _delivery(
    event: Mapping[str, object] | None = None,
    *,
    data: bytes | None = None,
    attrs: Mapping[str, str] | None = None,
) -> Delivery:
    body, a = encode(event or EVENTS[0])
    return Delivery(
        subscription="sub-x",
        message_id="m1",
        ack_id="a1",
        data=body if data is None else data,
        attributes={**a, **(attrs or {})},
        publish_time=datetime.now(UTC),
        delivery_attempt=2,
    )


class Recorder:
    name = "recorder"

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.seen: list[tuple[str, str | None, str | None]] = []

    def handle(self, event: DecodedEvent, delivery: Delivery) -> str:
        self.seen.append((event.event_id, current_trace_id(), current_correlation_id()))
        if self.error is not None:
            raise self.error
        return "applied"


def _worker(
    consumer: object, broker: MemoryBroker | None = None
) -> tuple[ConsumerWorker, MemoryBroker, StreamMetrics]:
    broker = broker or MemoryBroker(TOPO)
    metrics = StreamMetrics()
    router = DeadLetterRouter(broker, TOPO.dead_letter_topic)
    return ConsumerWorker(consumer, router, metrics), broker, metrics  # type: ignore[arg-type]


def test_success_acks_and_binds_the_events_trace_context() -> None:
    rec = Recorder()
    worker, _, metrics = _worker(rec)
    assert worker.process([_delivery()]) == [Disposition.ACK]
    assert rec.seen == [(EVENTS[0]["event_id"], EVENTS[0]["trace_id"], EVENTS[0]["correlation_id"])]
    assert current_trace_id() is None  # context does not leak
    assert metrics.count("sub-x", "status_applied") == 1
    assert metrics.count("sub-x", "redeliveries") == 1
    snap = metrics.snapshot()["sub-x"]["latency"]
    assert snap["processing"]["count"] == 1 and snap["end_to_end"]["count"] == 1


@pytest.mark.parametrize(
    ("data", "reason"),
    [(b"{oops", "malformed_json"), (b'{"schema_version": 9}', "unsupported_schema_version")],
)
def test_undecodable_message_goes_to_dlq_and_is_acked(data: bytes, reason: str) -> None:
    rec = Recorder()
    worker, broker, metrics = _worker(rec)
    assert worker.process([_delivery(data=data)]) == [Disposition.ACK]
    assert rec.seen == []
    (dl,) = broker.pull(DLQ)
    assert dl.data == data
    assert dl.attributes[DLQ_REASON] == reason
    assert dl.attributes[DLQ_SUBSCRIPTION] == "sub-x"
    assert dl.attributes[DLQ_ATTEMPT] == "2"
    assert dl.attributes["trace_id"] == EVENTS[0]["trace_id"]  # traceable via attributes
    assert metrics.count("sub-x", f"dead_lettered_{reason}") == 1


def test_permanent_error_dead_letters() -> None:
    worker, broker, _ = _worker(Recorder(PermanentError("unprocessable", "x")))
    assert worker.process([_delivery()]) == [Disposition.ACK]
    assert broker.pull(DLQ)[0].attributes[DLQ_REASON] == "unprocessable"


@pytest.mark.parametrize(
    ("error", "counter"),
    [(TransientError("db down"), "nack_transient"), (KeyError("bug"), "nack_error")],
)
def test_retryable_failures_nack(
    error: Exception, counter: str, caplog: pytest.LogCaptureFixture
) -> None:
    worker, broker, metrics = _worker(Recorder(error))
    with caplog.at_level(logging.WARNING):
        assert worker.process([_delivery()]) == [Disposition.NACK]
    assert broker.pull(DLQ) == []
    assert metrics.count("sub-x", counter) == 1
    assert any(getattr(r, "event_id", None) == EVENTS[0]["event_id"] for r in caplog.records)


def test_dlq_publish_failure_keeps_the_message() -> None:
    class BrokenPublisher:
        def publish(self, topic: str, data: bytes, attributes: Mapping[str, str]) -> str:
            raise ConnectionError("pubsub down")

        def publish_batch(
            self, topic: str, messages: Sequence[tuple[bytes, Mapping[str, str]]]
        ) -> list[str]:
            raise ConnectionError("pubsub down")

    metrics = StreamMetrics()
    worker = ConsumerWorker(Recorder(), DeadLetterRouter(BrokenPublisher(), "dlq"), metrics)
    assert worker.process([_delivery(data=b"{oops")]) == [Disposition.NACK]
    assert metrics.count("sub-x", "nack_dlq_publish_failed") == 1


class BatchRecorder(Recorder):
    def __init__(self, poison: str | None = None, transient: bool = False) -> None:
        super().__init__()
        self.poison = poison
        self.transient = transient
        self.batches: list[int] = []

    def handle(self, event: DecodedEvent, delivery: Delivery) -> str:
        return self.handle_batch([event])

    def handle_batch(self, events: Sequence[DecodedEvent]) -> str:
        if self.transient:
            raise TransientError("warehouse locked")
        if self.poison and any(e.event_id == self.poison for e in events):
            raise ValueError("poison")
        self.batches.append(len(events))
        return "applied"


def test_batch_applies_valid_and_dead_letters_undecodable() -> None:
    consumer = BatchRecorder()
    worker, broker, metrics = _worker(consumer)
    deliveries = [_delivery(e) for e in EVENTS[:3]] + [_delivery(data=b"junk")]
    assert worker.process(deliveries) == [Disposition.ACK] * 4
    assert consumer.batches == [3] and len(broker.pull(DLQ)) == 1
    assert metrics.count("sub-x", "batches") == 1


def test_batch_transient_failure_nacks_all() -> None:
    worker, _, _ = _worker(BatchRecorder(transient=True))
    assert worker.process([_delivery(e) for e in EVENTS[:3]]) == [Disposition.NACK] * 3


def test_batch_poison_is_isolated_one_by_one() -> None:
    consumer = BatchRecorder(poison=EVENTS[1]["event_id"])
    worker, _, metrics = _worker(consumer)
    out = worker.process([_delivery(e) for e in EVENTS[:3]])
    assert out == [Disposition.ACK, Disposition.NACK, Disposition.ACK]
    assert consumer.batches == [1, 1]
    assert metrics.count("sub-x", "batch_fallback") == 1
    assert metrics.count("sub-x", "deliveries") == 3  # fallback does not double count


def test_batch_of_only_undecodable_messages() -> None:
    worker, broker, _ = _worker(BatchRecorder())
    assert worker.process([_delivery(data=b"x"), _delivery(data=b"y")]) == [Disposition.ACK] * 2
    assert len(broker.pull(DLQ)) == 2


def test_transport_latency_prefers_producer_send_time() -> None:
    d = _delivery(attrs={ATTR_SENT_AT: "100.0"})
    assert transport_latency_ms(d, now=100.25) == pytest.approx(250.0)
    bad = _delivery(attrs={ATTR_SENT_AT: "not-a-number"})
    assert transport_latency_ms(bad, now=bad.publish_time.timestamp() + 1) == pytest.approx(1000.0)
    assert transport_latency_ms(_delivery(attrs={ATTR_SENT_AT: "200"}), now=100) == 0.0


def test_latency_sample_quantiles_and_reservoir() -> None:
    s = LatencySample()
    assert s.quantile(0.5) is None and s.summary()["max_ms"] is None
    for v in range(1, 101):
        s.add(float(v))
    assert (s.quantile(0.5), s.quantile(0.95), s.quantile(0.99)) == (50.0, 95.0, 99.0)
    import praxis.observability as m

    small = LatencySample(seed=1)
    old = m._RESERVOIR
    m._RESERVOIR = 10
    try:
        for v in range(1000):
            small.add(float(v))
    finally:
        m._RESERVOIR = old
    assert small.count == 1000 and len(small.values) == 10
