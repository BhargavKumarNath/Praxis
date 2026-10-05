"""In-process stream metrics: counters and latency samples per subscription.

Deliberately dependency-free; Phase 11 exports these through OpenTelemetry. Counters
count *deliveries and outcomes*, which include broker duplicates by design. Business
counts (unique events, money) come from the idempotent sinks, never from here.
"""

from __future__ import annotations

import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

# Re-exported for existing callers; the primitive lives in praxis.observability.
from praxis.observability import LatencySample as LatencySample


@dataclass
class StreamMetrics:
    counters: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    latency: dict[str, dict[str, LatencySample]] = field(default_factory=lambda: defaultdict(dict))
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def inc(self, subscription: str, name: str, n: int = 1) -> None:
        with self._lock:
            self.counters[subscription][name] += n

    def observe(self, subscription: str, name: str, value_ms: float) -> None:
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
