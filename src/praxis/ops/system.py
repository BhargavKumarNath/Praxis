"""One read of the whole system's metrics for alerting and dashboards (ADR 0020).

``SystemReader.read`` merges three inputs into the ``MetricsView`` that SLOs, alerts, the
health API and the AI operator evaluate:

1. the reader's own live telemetry;
2. the unexpired snapshots other processes published (``control.metric_snapshots``);
3. the pull collectors (``ops.collect``).

It also returns WHERE the numbers came from: each publishing process with its age and
whether it is stale, and each collector's status. A dashboard must show those inputs next
to the numbers, so a missing or stale input never looks like a healthy zero.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from praxis.control.metric_snapshots import FleetView, MetricSnapshotStore, SourceStatus
from praxis.control.store import ControlPlaneStore
from praxis.data.config import load_sources_config
from praxis.ops.collect import (
    DEFAULT_SETTINGS,
    STORE_ERRORS,
    Collector,
    CollectorSettings,
    dead_letter_collector,
    revenue_collector,
    run_collectors,
    warehouse_collector,
)
from praxis.telemetry import MetricsView, telemetry


@dataclass(frozen=True)
class SystemSnapshot:
    view: MetricsView
    scope: str  # "fleet" or "process" (snapshot store not configured / unavailable)
    processes: tuple[SourceStatus, ...] = ()
    collectors: dict[str, dict[str, Any]] = field(default_factory=dict)

    def inputs(self) -> dict[str, Any]:
        return {
            "telemetry_scope": self.scope,
            "processes": [p.as_dict() for p in self.processes],
            "stale_processes": [p.component for p in self.processes if p.stale],
            "collectors": self.collectors,
        }


class SystemReader:
    def __init__(
        self,
        snapshots: MetricSnapshotStore | None = None,
        collectors: Mapping[str, Collector] | None = None,
        *,
        own: tuple[str, str] | None = None,
    ) -> None:
        """``own`` = (component, instance) of the reader's published copy, which its live
        telemetry replaces (no double counting)."""
        self.snapshots = snapshots
        self.collectors = dict(collectors or {})
        self.own = own

    def read(self, now: datetime) -> SystemSnapshot:
        local = telemetry().snapshot()
        fleet, scope = FleetView(local), "process"
        if self.snapshots is not None:
            try:
                fleet, scope = self.snapshots.read(now, local=local, exclude=self.own), "fleet"
            except STORE_ERRORS:
                scope = "process (snapshot store unavailable)"
        collected = run_collectors(self.collectors, now)
        view = MetricsView(fleet.view.points + collected.view.points)
        return SystemSnapshot(view, scope, fleet.sources, collected.notes)


def build_reader(
    engine: Engine | None,
    warehouse_path: Path | None = None,
    *,
    own: tuple[str, str] | None = None,
    settings: CollectorSettings = DEFAULT_SETTINGS,
) -> SystemReader:
    """The standard reader: fleet snapshots and control-plane collectors when a database is
    configured, the warehouse collector when a warehouse path is given."""
    collectors: dict[str, Collector] = {}
    snapshots = None
    if engine is not None:
        snapshots = MetricSnapshotStore(engine)
        store = ControlPlaneStore(engine)
        collectors["dead_letters"] = dead_letter_collector(store)
        collectors["revenue"] = revenue_collector(store, settings)
    if warehouse_path is not None:
        collectors["warehouse"] = warehouse_collector(
            warehouse_path, load_sources_config(), settings
        )
    return SystemReader(snapshots, collectors, own=own)
