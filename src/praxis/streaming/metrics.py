"""In-process stream metrics: counters and latency samples per subscription.

Deliberately dependency-free; Phase 11 exports these through OpenTelemetry. Counters
count *deliveries and outcomes*, which include broker duplicates by design. Business
counts (unique events, money) come from the idempotent sinks, never from here.
"""

from __future__ import annotations

import math
import random
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

_RESERVOIR = 200_000


@dataclass
class LatencySample:
    """Bounded reservoir sample (seeded) of latencies in milliseconds."""

    seed: int = 0
    count: int = 0
    values: list[float] = field(default_factory=list)
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)  # noqa: S311 - sampling, not crypto

    def add(self, value_ms: float) -> None:
        self.count += 1
        if len(self.values) < _RESERVOIR:
            self.values.append(value_ms)
            return
        slot = self._rng.randrange(self.count)
        if slot < _RESERVOIR:
            self.values[slot] = value_ms

    def quantile(self, q: float) -> float | None:
        if not self.values:
            return None
        ordered = sorted(self.values)
        idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
        return ordered[idx]

    def summary(self) -> dict[str, float | int | None]:
        return {
            "count": self.count,
            "p50_ms": _round(self.quantile(0.50)),
            "p95_ms": _round(self.quantile(0.95)),
            "p99_ms": _round(self.quantile(0.99)),
            "max_ms": _round(max(self.values)) if self.values else None,
        }


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 3)


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
