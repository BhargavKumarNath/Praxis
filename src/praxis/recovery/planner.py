"""Retry planning: when (and whether) to retry, from P(collectible by t).

Pure functions. Elapsed time ``t`` is days since the invoice's first failed attempt. Given
the curve g(t) = P(C <= t | x) on whole days 0..H (g(0) = 0) and that every attempt so far
failed (the last at ``now``), the conditional curve is

    G(t) = (g(t) - g(now)) / (1 - g(now))   for t > now.

For a schedule now < t_1 < ... < t_m <= H (m <= remaining attempts), with G(t_0) = 0:

    value = sum_j [G(t_j) - G(t_{j-1})] * A * (1 - delay * t_j)     recovered at t_j
          - retry_cost  * sum_j (1 - G(t_{j-1}))                    retry j is executed
          - failed_cost * sum_j (1 - G(t_j))                        retry j fails

The plan maximises value over every schedule on the day grid; stopping (m = 0) is worth 0.
Only the first retry of a plan is ever acted on: after a failure the dunning service plans
again from the new ``now`` (with absorbing collectibility the continuation of an optimal plan
is optimal for the re-planned problem, so open-loop and closed-loop agree; property-tested).
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from numpy.typing import NDArray

from praxis.recovery.config import RecoveryPolicy

F64 = NDArray[np.float64]
I64 = NDArray[np.int64]


@dataclass(frozen=True, slots=True)
class Plan:
    retry_days: tuple[int, ...]  # elapsed days of every planned retry; empty = stop
    expected_value_minor: float
    p_next_success: float  # P(the next planned retry succeeds | every attempt so far failed)

    @property
    def next_retry_day(self) -> int | None:
        return self.retry_days[0] if self.retry_days else None


@lru_cache(maxsize=256)
def _schedules(first_day: int, horizon: int, max_retries: int) -> tuple[I64, ...]:
    """All increasing schedules on [first_day, horizon], grouped by length 1..max_retries."""
    days = range(first_day, horizon + 1)
    return tuple(
        np.array(list(itertools.combinations(days, m)), dtype=np.int64).reshape(-1, m)
        for m in range(1, max_retries + 1)
    )


def _values(cond: F64, sched: I64, amount: float, policy: RecoveryPolicy) -> F64:
    v = policy.valuation
    g = cond[sched]  # (k, m) conditional collectible probability at each retry
    prev = np.hstack([np.zeros((len(sched), 1)), g[:, :-1]])
    recovered = (g - prev) * amount * (1.0 - v.delay_cost_per_day * sched)
    executed = 1.0 - prev
    failed = 1.0 - g
    out: F64 = (
        recovered.sum(axis=1)
        - v.retry_cost_minor * executed.sum(axis=1)
        - v.failed_retry_cost_minor * failed.sum(axis=1)
    )
    return out


def conditional(curve: F64, now: float) -> F64:
    """G on the day grid given failure at ``now`` (days <= now map to 0)."""
    days = np.arange(len(curve), dtype=np.float64)
    g_now = float(np.interp(now, days, curve))
    denom = max(1.0 - g_now, 1e-12)
    out: F64 = np.where(days > now, np.clip((curve - g_now) / denom, 0.0, 1.0), 0.0)
    return out


def plan_retries(
    curve: F64,
    *,
    now: float,
    attempts_made: int,
    amount_minor: int,
    policy: RecoveryPolicy,
) -> Plan:
    """Best schedule of the remaining retries. ``curve[d]`` = P(C <= d) for d = 0..horizon."""
    horizon = policy.bounds.horizon_days
    if len(curve) != horizon + 1:
        raise ValueError(f"curve must cover days 0..{horizon}")
    remaining = policy.bounds.max_attempts - attempts_made
    first_day = math.floor(now) + 1
    if remaining <= 0 or first_day > horizon:
        return Plan((), 0.0, 0.0)
    cond = conditional(np.asarray(curve, dtype=np.float64), now)
    best: tuple[float, int, tuple[int, ...]] = (0.0, 0, ())
    for sched in _schedules(first_day, horizon, remaining):
        values = _values(cond, sched, float(amount_minor), policy)
        k = int(np.argmax(values))  # first maximum: earliest schedule among ties
        candidate = (float(values[k]), sched.shape[1], tuple(int(d) for d in sched[k]))
        # strictly better value wins; on a tie, fewer retries win (shorter lengths come first)
        if candidate[0] > best[0] + 1e-9:
            best = candidate
    value, _, days = best
    p_next = float(cond[days[0]]) if days else 0.0
    return Plan(days, value, p_next)


def baseline_next_day(
    policy: RecoveryPolicy, attempts_made: int, last_attempt_day: float
) -> float | None:
    """The deterministic schedule: next retry ``offset`` days after the previous attempt."""
    offsets = policy.baseline.retry_offsets_days
    if attempts_made < 1 or attempts_made > len(offsets):
        return None
    return last_attempt_day + offsets[attempts_made - 1]


def schedule_value(
    curve: F64, days: tuple[int, ...], *, now: float, amount_minor: int, policy: RecoveryPolicy
) -> float:
    """Expected value of a given schedule (used to compare plans and in tests)."""
    if not days:
        return 0.0
    cond = conditional(np.asarray(curve, dtype=np.float64), now)
    sched = np.array([days], dtype=np.int64)
    return float(_values(cond, sched, float(amount_minor), policy)[0])
