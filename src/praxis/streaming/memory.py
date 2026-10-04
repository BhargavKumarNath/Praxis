"""Deterministic in-memory broker with Pub/Sub semantics and seeded fault injection.

Used for unit, property and chaos tests, and for local pipeline runs. It reproduces the
behaviours correctness must not depend on, but must survive:

* at-least-once delivery: a message is redelivered until acked; ``FaultPlan`` adds
  independent duplicate copies that can arrive even after the original was acked;
* leases: an unacked delivery expires after the ack deadline and is redelivered;
* retry policy: exponential backoff between attempts, capped;
* dead-lettering: after ``max_delivery_attempts`` the message is forwarded to the DLQ
  topic with Pub/Sub's ``CloudPubSubDeadLetter*`` attributes;
* delay and reordering: seeded random availability delay and random pull order.

Time is a ``VirtualClock``; nothing sleeps. Same seed and inputs give the same delivery
sequence, so failures are reproducible.
"""

from __future__ import annotations

import heapq
import itertools
import random
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from praxis.streaming.topology import SubscriptionSpec, Topology
from praxis.streaming.transport import (
    DEAD_LETTER_SOURCE_DELIVERY_COUNT,
    DEAD_LETTER_SOURCE_SUBSCRIPTION,
    ConsumerCrashed,
    Delivery,
    Disposition,
    Worker,
)

DEFAULT_EPOCH = 1_767_225_600.0  # 2026-01-01T00:00:00Z


class VirtualClock:
    def __init__(self, start: float = DEFAULT_EPOCH) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance_to(self, when: float) -> None:
        if when > self._now:
            self._now = when

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("time cannot move backwards")
        self._now += seconds


@dataclass(frozen=True)
class FaultPlan:
    seed: int = 0
    duplicate_rate: float = 0.0
    max_delay_s: float = 0.0
    reorder: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.duplicate_rate <= 1.0:
            raise ValueError("duplicate_rate must be in [0, 1]")
        if self.max_delay_s < 0:
            raise ValueError("max_delay_s must be >= 0")


@dataclass(frozen=True, slots=True)
class _Message:
    message_id: str
    data: bytes
    attributes: Mapping[str, str]
    publish_time: float


@dataclass(slots=True)
class _Copy:
    copy_id: int
    msg: _Message
    available_at: float
    attempts: int = 0
    lease_until: float = 0.0
    lease_token: int = 0


@dataclass
class _Sub:
    spec: SubscriptionSpec
    waiting: list[tuple[float, int, _Copy]] = field(default_factory=list)  # heap
    ready: list[_Copy] = field(default_factory=list)  # reorder mode only
    leased: dict[int, _Copy] = field(default_factory=dict)
    stats: Counter[str] = field(default_factory=Counter)


class MemoryBroker:
    def __init__(
        self,
        topology: Topology,
        *,
        clock: VirtualClock | None = None,
        faults: FaultPlan | None = None,
    ) -> None:
        self.topology = topology
        self.clock = clock or VirtualClock()
        self.faults = faults or FaultPlan()
        self._rng = random.Random(self.faults.seed)  # noqa: S311 - fault injection, not crypto
        self._ids = itertools.count(1)
        self._seq = itertools.count()
        self._subs = {s.name: _Sub(s) for s in topology.subscriptions}
        self._by_topic: dict[str, list[_Sub]] = {t: [] for t in topology.topics}
        for sub in self._subs.values():
            self._by_topic[sub.spec.topic].append(sub)
        self.published: Counter[str] = Counter()

    # --- publishing -------------------------------------------------------------------
    def publish(self, topic: str, data: bytes, attributes: Mapping[str, str]) -> str:
        if topic not in self._by_topic:
            raise KeyError(f"unknown topic {topic}")
        msg = _Message(str(next(self._ids)), bytes(data), dict(attributes), self.clock())
        self.published[topic] += 1
        for sub in self._by_topic[topic]:
            if not sub.spec.matches(dict(attributes)):
                sub.stats["filtered"] += 1
                continue
            copies = 1 + (self._rng.random() < self.faults.duplicate_rate)
            for _ in range(copies):
                delay = (
                    self._rng.uniform(0, self.faults.max_delay_s) if self.faults.max_delay_s else 0
                )
                self._schedule(sub, _Copy(next(self._ids), msg, self.clock() + delay))
            sub.stats["enqueued"] += copies
        return msg.message_id

    def publish_batch(
        self, topic: str, messages: Sequence[tuple[bytes, Mapping[str, str]]]
    ) -> list[str]:
        return [self.publish(topic, data, attrs) for data, attrs in messages]

    def _schedule(self, sub: _Sub, copy: _Copy) -> None:
        heapq.heappush(sub.waiting, (copy.available_at, next(self._seq), copy))

    # --- consuming --------------------------------------------------------------------
    def pull(self, subscription: str, max_messages: int = 100) -> list[Delivery]:
        sub = self._subs[subscription]
        self._expire_leases(sub)
        now = self.clock()
        out: list[Delivery] = []
        while len(out) < max_messages:
            copy = self._next_ready(sub, now)
            if copy is None:
                break
            copy.attempts += 1
            copy.lease_token += 1
            copy.lease_until = now + sub.spec.ack_deadline_s
            sub.leased[copy.copy_id] = copy
            sub.stats["delivered"] += 1
            if copy.attempts > 1:
                sub.stats["redelivered"] += 1
            out.append(
                Delivery(
                    subscription=subscription,
                    message_id=copy.msg.message_id,
                    ack_id=f"{copy.copy_id}:{copy.lease_token}",
                    data=copy.msg.data,
                    attributes=copy.msg.attributes,
                    publish_time=datetime.fromtimestamp(copy.msg.publish_time, tz=UTC),
                    delivery_attempt=copy.attempts,
                )
            )
        return out

    def _next_ready(self, sub: _Sub, now: float) -> _Copy | None:
        if not self.faults.reorder:
            if sub.waiting and sub.waiting[0][0] <= now:
                return heapq.heappop(sub.waiting)[2]
            return None
        while sub.waiting and sub.waiting[0][0] <= now:
            sub.ready.append(heapq.heappop(sub.waiting)[2])
        if not sub.ready:
            return None
        i = self._rng.randrange(len(sub.ready))
        sub.ready[i], sub.ready[-1] = sub.ready[-1], sub.ready[i]
        return sub.ready.pop()

    def ack(self, subscription: str, ack_ids: Iterable[str]) -> None:
        sub = self._subs[subscription]
        for ack_id in ack_ids:
            copy = self._lease(sub, ack_id)
            if copy is None:
                sub.stats["stale_ack"] += 1
                continue
            del sub.leased[copy.copy_id]
            sub.stats["acked"] += 1

    def nack(self, subscription: str, ack_ids: Iterable[str]) -> None:
        sub = self._subs[subscription]
        for ack_id in ack_ids:
            copy = self._lease(sub, ack_id)
            if copy is None:
                sub.stats["stale_nack"] += 1
                continue
            del sub.leased[copy.copy_id]
            sub.stats["nacked"] += 1
            self._failed(sub, copy)

    def _lease(self, sub: _Sub, ack_id: str) -> _Copy | None:
        copy_id, token = (int(x) for x in ack_id.split(":"))
        copy = sub.leased.get(copy_id)
        if copy is None or copy.lease_token != token:
            return None
        return copy

    def _expire_leases(self, sub: _Sub) -> None:
        now = self.clock()
        for copy in [c for c in sub.leased.values() if c.lease_until <= now]:
            del sub.leased[copy.copy_id]
            sub.stats["lease_expired"] += 1
            self._failed(sub, copy)

    def _failed(self, sub: _Sub, copy: _Copy) -> None:
        spec = sub.spec
        if spec.max_delivery_attempts is not None and copy.attempts >= spec.max_delivery_attempts:
            sub.stats["dead_lettered"] += 1
            assert spec.dead_letter_topic is not None  # noqa: S101 - enforced by topology
            attrs = dict(copy.msg.attributes)
            attrs[DEAD_LETTER_SOURCE_SUBSCRIPTION] = spec.name
            attrs[DEAD_LETTER_SOURCE_DELIVERY_COUNT] = str(copy.attempts)
            self.publish(spec.dead_letter_topic, copy.msg.data, attrs)
            return
        copy.available_at = self.clock() + spec.backoff_s(copy.attempts)
        self._schedule(sub, copy)

    # --- introspection ----------------------------------------------------------------
    def next_wakeup(self, subscription: str) -> float | None:
        sub = self._subs[subscription]
        times = [c.lease_until for c in sub.leased.values()]
        if sub.waiting:
            times.append(sub.waiting[0][0])
        if sub.ready:
            times.append(self.clock())
        return min(times) if times else None

    def backlog(self, subscription: str) -> int:
        sub = self._subs[subscription]
        return len(sub.waiting) + len(sub.ready) + len(sub.leased)

    def stats(self, subscription: str) -> dict[str, int]:
        return dict(self._subs[subscription].stats)


@dataclass
class DrainReport:
    deliveries: int = 0
    acks: int = 0
    nacks: int = 0
    crashes: int = 0
    rounds: int = 0


def drain(
    broker: MemoryBroker,
    subscription: str,
    worker_factory: Callable[[], Worker],
    *,
    batch_size: int = 100,
    max_rounds: int = 10_000_000,
) -> DrainReport:
    """Deliver until the subscription is empty, advancing virtual time when idle.

    Batch workers get the whole pull and are acked together; other workers are acked one
    message at a time as each completes (like the Pub/Sub streaming-pull client), so a
    crash only strands the messages that were still in flight.

    ``ConsumerCrashed`` models process death: un-acked in-flight messages are neither acked
    nor nacked (their leases expire, their delivery attempt counts grow) and the worker is
    rebuilt from the factory, losing in-memory state, like a restarted container.
    """
    report = DrainReport()
    worker = worker_factory()
    while report.rounds < max_rounds:
        report.rounds += 1
        deliveries = broker.pull(subscription, batch_size)
        if not deliveries:
            wake = broker.next_wakeup(subscription)
            if wake is None:
                return report
            broker.clock.advance_to(wake)
            continue
        report.deliveries += len(deliveries)
        groups = [deliveries] if worker.batches else [[d] for d in deliveries]
        try:
            for group in groups:
                dispositions = worker.process(group)
                acks = [
                    d.ack_id
                    for d, s in zip(group, dispositions, strict=True)
                    if s is Disposition.ACK
                ]
                nacks = [
                    d.ack_id
                    for d, s in zip(group, dispositions, strict=True)
                    if s is Disposition.NACK
                ]
                broker.ack(subscription, acks)
                broker.nack(subscription, nacks)
                report.acks += len(acks)
                report.nacks += len(nacks)
        except ConsumerCrashed:
            report.crashes += 1
            worker = worker_factory()
    raise RuntimeError(f"drain of {subscription} did not finish within {max_rounds} rounds")
