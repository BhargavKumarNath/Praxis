"""Dependency-free metric primitives shared by every service (Phase 11 exports via OTel)."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

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
