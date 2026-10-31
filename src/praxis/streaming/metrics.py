"""In-process stream metrics: counters and latency samples per subscription.

Deliberately dependency-free; Phase 11 exports these through OpenTelemetry. Counters
count *deliveries and outcomes*, which include broker duplicates by design. Business
counts (unique events, money) come from the idempotent sinks, never from here.

Counter increments are aggregated locally and pushed to OpenTelemetry in bulk (every
``FLUSH_EVERY`` increments, and before every ``Telemetry.snapshot``), because one OTel
``add`` per delivery cost about a third of consumer throughput (ADR 0022).
"""

from __future__ import annotations

import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from praxis.observability import LatencySample
from praxis.telemetry import Telemetry, telemetry

FLUSH_EVERY = 1024


@dataclass
class StreamMetrics:
    counters: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    latency: dict[str, dict[str, LatencySample]] = field(default_factory=lambda: defaultdict(dict))
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _pending: Counter[tuple[str, str]] = field(default_factory=Counter, repr=False)
    _pending_ops: int = field(default=0, repr=False)
    _sink: Telemetry | None = field(default=None, repr=False)

    def inc(self, subscription: str, name: str, n: int = 1) -> None:
        sink = telemetry()
        with self._lock:
            self.counters[subscription][name] += n
            if sink is not self._sink:  # first use, or the instance was swapped (tests)
                self._flush_locked()
                self._sink = sink
                sink.register_pre_collect(self.flush)
            self._pending[(subscription, name)] += n
            self._pending_ops += 1
            if self._pending_ops >= FLUSH_EVERY:
                self._flush_locked()

    def flush(self) -> None:
        """Push aggregated counter deltas to OpenTelemetry (``events_consumed_total``)."""
        with self._lock:
            self._flush_locked()

    def __del__(self) -> None:  # an instance going away must not lose its buffered counts
        self._flush_locked()

    def _flush_locked(self) -> None:
        if self._sink is not None:
            for (subscription, name), n in self._pending.items():
                self._sink.inc("events_consumed_total", n, subscription=subscription, outcome=name)
        self._pending.clear()
        self._pending_ops = 0

    def observe(self, subscription: str, name: str, value_ms: float) -> None:
        if name == "end_to_end":
            telemetry().observe("event_lag_ms", value_ms, subscription=subscription)
        with self._lock:
            sample = self.latency[subscription].get(name)
            if sample is None:
                sample = self.latency[subscription][name] = LatencySample()
            sample.add(value_ms)

    def count(self, subscription: str, name: str) -> int:
        with self._lock:
            return self.counters[subscription][name]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        subs = sorted(set(self.counters) | set(self.latency))
        return {
            s: {
                "counters": dict(sorted(self.counters[s].items())),
                "latency": {k: v.summary() for k, v in sorted(self.latency[s].items())},
            }
            for s in subs
        }


def record_backlog(backlog: dict[str, int]) -> None:
    """``queue_backlog`` per subscription, sampled from a broker that can count it (the
    in-memory broker). Dead letters are not passed here: they are ``dlq_backlog``, counted
    from the control plane by the ops collectors (ADR 0020)."""
    for subscription, n in backlog.items():
        telemetry().set("queue_backlog", n, subscription=subscription)
