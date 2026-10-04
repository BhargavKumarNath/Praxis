"""In-memory broker semantics: it must behave like Pub/Sub for the properties we rely on."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from praxis.streaming.memory import FaultPlan, MemoryBroker, VirtualClock, drain
from praxis.streaming.topology import DLQ_INSPECT, OPERATIONAL, WAREHOUSE, build_topology
from praxis.streaming.transport import (
    DEAD_LETTER_SOURCE_DELIVERY_COUNT,
    DEAD_LETTER_SOURCE_SUBSCRIPTION,
    ConsumerCrashed,
    Delivery,
    Disposition,
)

TOPO = build_topology(min_backoff_s=10, max_backoff_s=60, ack_deadline_s=30)
EVENTS = TOPO.events_topic
WH = TOPO.by_role(WAREHOUSE).name
OPS = TOPO.by_role(OPERATIONAL).name
DLQ = TOPO.by_role(DLQ_INSPECT).name


def _broker(**faults: object) -> MemoryBroker:
    return MemoryBroker(TOPO, faults=FaultPlan(**faults))  # type: ignore[arg-type]


def test_ack_removes_message() -> None:
    b = _broker()
    b.publish(EVENTS, b"m1", {})
    (d,) = b.pull(WH)
    assert d.delivery_attempt == 1 and d.data == b"m1"
    b.ack(WH, [d.ack_id])
    assert b.backlog(WH) == 0 and b.next_wakeup(WH) is None


def test_nack_redelivers_after_backoff_with_incremented_attempt() -> None:
    b = _broker()
    b.publish(EVENTS, b"m1", {})
    (d,) = b.pull(WH)
    b.nack(WH, [d.ack_id])
    assert b.pull(WH) == []  # backoff not elapsed
    assert b.next_wakeup(WH) == b.clock() + 10
    b.clock.advance(10)
    (d2,) = b.pull(WH)
    assert d2.delivery_attempt == 2 and d2.message_id == d.message_id


def test_unacked_lease_expires_and_is_redelivered() -> None:
    b = _broker()
    b.publish(EVENTS, b"m1", {})
    (d,) = b.pull(WH)
    b.clock.advance(30)  # ack deadline: consumer died
    assert b.pull(WH) == []  # expiry schedules a backoff
    b.clock.advance(10)
    (d2,) = b.pull(WH)
    assert d2.delivery_attempt == 2
    b.ack(WH, [d.ack_id])  # the dead consumer's stale ack must not ack the new lease
    assert b.stats(WH)["stale_ack"] == 1 and b.backlog(WH) == 1
    b.nack(WH, ["999:1"])
    assert b.stats(WH)["stale_nack"] == 1


def test_dead_letter_after_max_delivery_attempts() -> None:
    b = _broker()
    b.publish(EVENTS, b"poison", {"event_id": "e1"})
    attempts = 0
    while b.backlog(WH):
        for d in b.pull(WH):
            attempts += 1
            b.nack(WH, [d.ack_id])
        b.clock.advance(60)
    assert attempts == 5  # bounded: exactly max_delivery_attempts
    (dl,) = b.pull(DLQ)
    assert dl.data == b"poison"
    assert dl.attributes[DEAD_LETTER_SOURCE_SUBSCRIPTION] == WH
    assert dl.attributes[DEAD_LETTER_SOURCE_DELIVERY_COUNT] == "5"
    assert dl.attributes["event_id"] == "e1"
    assert b.stats(WH)["dead_lettered"] == 1


def test_filter_routes_by_attribute() -> None:
    b = _broker()
    b.publish(EVENTS, b"bulk", {"stateful": "false"})
    b.publish(EVENTS, b"state", {"stateful": "true"})
    assert [d.data for d in b.pull(OPS)] == [b"state"]
    assert len(b.pull(WH)) == 2
    assert b.stats(OPS)["filtered"] == 1


def test_duplicates_are_independent_copies_with_the_same_message_id() -> None:
    b = _broker(seed=1, duplicate_rate=1.0)
    b.publish(EVENTS, b"m1", {})
    first = b.pull(WH, 1)
    b.ack(WH, [first[0].ack_id])
    (dup,) = b.pull(WH)  # arrives after the original was acked
    assert dup.message_id == first[0].message_id and dup.delivery_attempt == 1


def test_delay_and_reorder_are_seeded_and_reproducible() -> None:
    def order(seed: int) -> list[bytes]:
        b = _broker(seed=seed, max_delay_s=100, reorder=True)
        for i in range(50):
            b.publish(EVENTS, str(i).encode(), {})
        out: list[bytes] = []
        while b.backlog(WH):
            ds = b.pull(WH, 7)
            if not ds:
                b.clock.advance_to(b.next_wakeup(WH) or 0)
            b.ack(WH, [d.ack_id for d in ds])
            out.extend(d.data for d in ds)
        return out

    assert order(3) == order(3)
    assert order(3) != order(4)
    assert sorted(order(3)) == sorted(str(i).encode() for i in range(50))
    assert order(3) != [str(i).encode() for i in range(50)]


def test_unknown_topic_and_bad_faults_are_rejected() -> None:
    with pytest.raises(KeyError):
        _broker().publish("nope", b"", {})
    with pytest.raises(ValueError):
        FaultPlan(duplicate_rate=1.5)
    with pytest.raises(ValueError):
        FaultPlan(max_delay_s=-1)
    with pytest.raises(ValueError):
        VirtualClock().advance(-1)


class _Flaky:
    batches = True

    def __init__(self, crash_on: set[bytes]) -> None:
        self.crash_on = crash_on
        self.seen: list[tuple[bytes, int]] = []

    def process(self, deliveries: Sequence[Delivery]) -> list[Disposition]:
        for d in deliveries:
            self.seen.append((d.data, d.delivery_attempt))
            if d.data in self.crash_on:
                self.crash_on.discard(d.data)
                raise ConsumerCrashed("boom")
        return [Disposition.ACK] * len(deliveries)


def test_drain_restarts_crashed_worker_and_redelivers_whole_batch() -> None:
    b = _broker()
    for i in range(5):
        b.publish(EVENTS, str(i).encode(), {})
    workers: list[_Flaky] = []
    crash = {b"2"}

    def factory() -> _Flaky:
        workers.append(_Flaky(crash))
        return workers[-1]

    report = drain(b, WH, factory, batch_size=10)
    assert report.crashes == 1 and len(workers) == 2
    assert report.acks == 5 and b.backlog(WH) == 0
    redelivered = {data for data, attempt in workers[1].seen if attempt == 2}
    assert redelivered == {str(i).encode() for i in range(5)}  # nothing in the batch was acked


def test_non_batch_worker_is_acked_message_by_message() -> None:
    b = _broker()
    for i in range(4):
        b.publish(EVENTS, str(i).encode(), {})

    class CrashOnThird:
        batches = False
        crashed = False

        def process(self, deliveries: Sequence[Delivery]) -> list[Disposition]:
            if deliveries[0].data == b"2" and not CrashOnThird.crashed:
                CrashOnThird.crashed = True
                raise ConsumerCrashed("boom")
            return [Disposition.ACK]

    report = drain(b, WH, CrashOnThird, batch_size=10)
    assert report.crashes == 1 and report.acks == 4
    # "0" and "1" were acked before the crash and never redelivered.
    assert b.stats(WH)["redelivered"] == 2


def test_drain_gives_up_after_max_rounds() -> None:
    b = _broker()
    b.publish(EVENTS, b"x", {})

    class Never:
        batches = False

        def process(self, deliveries: Sequence[Delivery]) -> list[Disposition]:
            return [Disposition.NACK] * len(deliveries)

    with pytest.raises(RuntimeError, match="did not finish"):
        drain(b, WH, Never, max_rounds=2)
