"""Pull collectors: metrics derived from durable state at each ops tick (ADR 0020).

Some signals belong to no long-running process, so nothing could push them: the age of
each external source and its contract failures (the warehouse), the dead-letter backlog
(the control plane) and the revenue KPI (the payment ledger). The ops monitor derives them
from the stores of record instead. Each collector writes through a fresh, catalogue-checked
``Telemetry`` and reports its own status; one failing store (a locked warehouse file, a
database outage) marks only its collector ``unavailable`` and never stops the tick.

Revenue comes from the SIMULATOR's ledger in this repository: it is synthetic, never a
real-world business result, and its days are simulated days, not wall-clock days.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
from sqlalchemy import exc

from praxis.control.store import ControlPlaneStore
from praxis.data.config import SourcesConfig
from praxis.data.freshness import FreshnessState, check_freshness
from praxis.data.models import SourceId
from praxis.data.warehouse import Warehouse
from praxis.errors import TransientError
from praxis.ops.signals import record_kpi
from praxis.telemetry import MetricsView, Telemetry

Collector = Callable[[Telemetry, datetime], dict[str, Any]]

# Store failures a collector reports as "unavailable"; anything else is a bug and raises.
STORE_ERRORS = (TransientError, exc.SQLAlchemyError, duckdb.Error, OSError)


@dataclass(frozen=True)
class CollectorSettings:
    """Measurement windows (pre-registered in ADR 0020; alert thresholds stay in
    ``configs/ops``). A quarantined batch counts as a contract failure for one day."""

    contract_window: timedelta = timedelta(hours=24)
    kpi_history_days: int = 28
    kpi_min_history: int = 7


DEFAULT_SETTINGS = CollectorSettings()


@dataclass(frozen=True)
class Collected:
    view: MetricsView
    notes: dict[str, dict[str, Any]]


def run_collectors(collectors: Mapping[str, Collector], now: datetime) -> Collected:
    tel = Telemetry()
    notes: dict[str, dict[str, Any]] = {}
    for name, collect in collectors.items():
        try:
            notes[name] = {"status": "ok", **collect(tel, now)}
        except STORE_ERRORS as error:
            notes[name] = {"status": "unavailable", "error": type(error).__name__}
    return Collected(tel.snapshot(), notes)


def warehouse_collector(
    path: Path, sources: SourcesConfig, settings: CollectorSettings = DEFAULT_SETTINGS
) -> Collector:
    """``data_source_age_s`` (newest observation, per source) and
    ``data_contract_failures_total`` (quarantined batches in the window, 0 included)."""

    def collect(tel: Telemetry, now: datetime) -> dict[str, Any]:
        with Warehouse(path, read_only=True) as wh:
            results = check_freshness(wh, sources, now)
            failures = wh.quarantined_batches(now - settings.contract_window, now)
        for r in results:
            if r.latest_observation is not None:
                age = max((now - r.latest_observation).total_seconds(), 0.0)
                tel.set("data_source_age_s", age, source=r.source.value)
        for source in SourceId:
            tel.inc("data_contract_failures_total", failures.get(source.value, 0),
                    source=source.value)  # fmt: skip
        return {
            "sources": {r.source.value: r.state.value for r in results},
            "missing": [r.source.value for r in results if r.state is FreshnessState.MISSING],
            "contract_window_h": settings.contract_window.total_seconds() / 3600,
        }

    return collect


def dead_letter_collector(store: ControlPlaneStore) -> Collector:
    """``dlq_backlog``: open (not redriven) dead letters per source subscription."""

    def collect(tel: Telemetry, _now: datetime) -> dict[str, Any]:
        backlog = store.dead_letter_backlog()
        for sub, n in backlog.items():
            tel.set("dlq_backlog", n, subscription=sub)
        return {"subscriptions": len(backlog)}

    return collect


def revenue_collector(
    store: ControlPlaneStore, settings: CollectorSettings = DEFAULT_SETTINGS
) -> Collector:
    """``business_kpi_deviation{kpi="daily_revenue_<ccy>"}``: the last complete ledger day
    against the median of the days before it (``signals.record_kpi``). SYNTHETIC."""

    def collect(tel: Telemetry, _now: datetime) -> dict[str, Any]:
        series = store.daily_paid_revenue(settings.kpi_history_days + 1)
        out: dict[str, Any] = {"origin": "synthetic (simulator ledger)", "kpis": {}}
        for currency, days in series.items():
            *history, (day, value) = days
            kpi = f"daily_revenue_{currency.lower()}"
            deviation = record_kpi(tel, kpi, float(value), [float(v) for _, v in history],
                                   min_history=settings.kpi_min_history)  # fmt: skip
            out["kpis"][kpi] = {"day": day.isoformat(), "value_minor": value,
                                "history_days": len(history), "deviation": deviation}  # fmt: skip
        return out

    return collect
