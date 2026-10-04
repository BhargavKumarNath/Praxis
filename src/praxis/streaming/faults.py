"""Fault injection for consumers and the database boundary (chaos tests, local runs).

* ``CrashPlan`` / ``CrashingConsumer``: the process dies once before, or once after, the
  side effect of selected events. The plan lives outside the consumer, so it survives the
  restart that ``drain`` performs (otherwise the restarted consumer would crash forever).
* ``DatabaseOutage``: makes the next N statements on an engine fail with a real
  ``sqlalchemy.exc.OperationalError`` (what a dropped connection raises).

Selection is a pure function of ``(seed, event_id)``, so runs are reproducible.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, event, exc

from praxis.events.codec import DecodedEvent
from praxis.streaming.runtime import BatchConsumer, Consumer
from praxis.streaming.transport import ConsumerCrashed, Delivery


def _unit(seed: int, tag: str, event_id: str) -> float:
    digest = hashlib.blake2b(f"{seed}:{tag}:{event_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest) / 2**64


@dataclass
class CrashPlan:
    seed: int = 0
    before_rate: float = 0.0
    after_rate: float = 0.0
    before: set[str] = field(default_factory=set)
    after: set[str] = field(default_factory=set)
    crashed_before: set[str] = field(default_factory=set)
    crashed_after: set[str] = field(default_factory=set)

    def should_crash_before(self, event_id: str) -> bool:
        if event_id in self.crashed_before:
            return False
        hit = event_id in self.before or _unit(self.seed, "b", event_id) < self.before_rate
        if hit:
            self.crashed_before.add(event_id)
        return hit

    def should_crash_after(self, event_id: str) -> bool:
        if event_id in self.crashed_after:
            return False
        hit = event_id in self.after or _unit(self.seed, "a", event_id) < self.after_rate
        if hit:
            self.crashed_after.add(event_id)
        return hit


class CrashingConsumer:
    def __init__(self, inner: Consumer, plan: CrashPlan) -> None:
        self.inner = inner
        self.plan = plan
        self.name = inner.name

    def handle(self, event: DecodedEvent, delivery: Delivery) -> str:
        if self.plan.should_crash_before(event.event_id):
            raise ConsumerCrashed(f"crash before side effect of {event.event_id}")
        status = self.inner.handle(event, delivery)
        if self.plan.should_crash_after(event.event_id):
            raise ConsumerCrashed(f"crash after side effect of {event.event_id}, before ack")
        return status


class CrashingBatchConsumer(CrashingConsumer):
    def __init__(self, inner: BatchConsumer, plan: CrashPlan) -> None:
        super().__init__(inner, plan)
        self._batch = inner

    def handle_batch(self, events: Sequence[DecodedEvent]) -> str:
        for e in events:
            if self.plan.should_crash_before(e.event_id):
                raise ConsumerCrashed(f"crash before side effect of batch with {e.event_id}")
        status = self._batch.handle_batch(events)
        for e in events:
            if self.plan.should_crash_after(e.event_id):
                raise ConsumerCrashed(f"crash after side effect of batch with {e.event_id}")
        return status


def with_crashes(inner: Consumer, plan: CrashPlan) -> Consumer:
    """Wrap ``inner``, preserving whether it supports batches."""
    if hasattr(inner, "handle_batch"):
        return CrashingBatchConsumer(inner, plan)  # type: ignore[arg-type]
    return CrashingConsumer(inner, plan)


class DatabaseOutage:
    """Let ``after`` statements through, fail the next ``statements``, then recover."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.skip = 0
        self.remaining = 0
        self.injected = 0
        event.listen(engine, "before_cursor_execute", self._before)

    def fail_next(self, statements: int, *, after: int = 0) -> None:
        self.skip = after
        self.remaining = statements

    def _before(self, *args: Any, **kwargs: Any) -> None:
        if self.skip > 0:
            self.skip -= 1
            return
        if self.remaining > 0:
            self.remaining -= 1
            self.injected += 1
            raise exc.OperationalError(
                "injected", None, ConnectionError("injected database outage")
            )

    def remove(self) -> None:
        event.remove(self._engine, "before_cursor_execute", self._before)
