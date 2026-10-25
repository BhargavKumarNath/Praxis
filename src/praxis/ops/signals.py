"""Gauges that need a small computation before they can be alerted on.

* ``kpi_deviation``: relative deviation of today's KPI from the median of its trailing
  window (median, not mean, so one earlier shock does not become the new baseline).
* ``sample_backlog``: queue depth and dead-letter backlog per subscription, from callables
  the owner of the queue provides (the in-memory broker, the control-plane store). On
  Cloud Pub/Sub these gauges come from Cloud Monitoring instead (documented in the
  Pub/Sub backlog runbook).

KPI values are whatever the caller feeds; in this repository the revenue series comes from
the SIMULATOR and is synthetic, never a real-world business result.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence

from praxis.telemetry import Telemetry


def kpi_deviation(value: float, history: Sequence[float], *, min_history: int = 7) -> float | None:
    """(value - baseline) / baseline, or None when there is not enough history."""
    if len(history) < min_history:
        return None
    baseline = statistics.median(history)
    if baseline == 0:
        return None
    return (value - baseline) / abs(baseline)


def record_kpi(
    tel: Telemetry, kpi: str, value: float, history: Sequence[float], *, min_history: int = 7
) -> float | None:
    deviation = kpi_deviation(value, history, min_history=min_history)
    if deviation is not None:
        tel.set("business_kpi_deviation", deviation, kpi=kpi)
    return deviation


def sample_backlog(
    tel: Telemetry,
    subscriptions: Sequence[str],
    backlog: Callable[[str], int],
    dead_letters: Callable[[str], int],
) -> None:
    for sub in subscriptions:
        tel.set("queue_backlog", backlog(sub), subscription=sub)
        tel.set("dlq_backlog", dead_letters(sub), subscription=sub)
