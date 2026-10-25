"""One evaluation tick: metrics -> health report -> alerts -> incidents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from praxis.ops.config import Category, OpsConfig
from praxis.ops.evaluate import Alert
from praxis.ops.health import HealthReport, build_health
from praxis.ops.incidents import IncidentManager, IncidentReport
from praxis.ops.system import SystemReader
from praxis.telemetry import telemetry


@dataclass(frozen=True)
class Tick:
    health: HealthReport
    incidents: IncidentReport


class OpsMonitor:
    def __init__(
        self, cfg: OpsConfig, manager: IncidentManager, reader: SystemReader | None = None
    ) -> None:
        """Without a ``reader`` the tick sees only this process's telemetry."""
        self.cfg = cfg
        self.manager = manager
        self.reader = reader

    def tick(self, now: datetime | None = None, *, extra: list[Alert] | None = None) -> Tick:
        now = now or datetime.now(UTC)
        if self.reader is None:
            view, inputs = telemetry().snapshot(), None
        else:
            system = self.reader.read(now)
            view, inputs = system.view, system.inputs()
        health = build_health(self.cfg, view, self.manager.store.live(), now=now, inputs=inputs)
        report = self.manager.process(health.alerts() + (extra or []), now)
        return Tick(health, report)


def synthetic_alert(cfg: OpsConfig, category: Category) -> Alert:
    """A clearly labelled test alert built from the first rule of ``category``.

    It carries that rule's real runbook and severity, so the incident it opens exercises
    the same path as a genuine alert. It is marked ``synthetic`` and has its own
    fingerprint, so it never merges with (or hides) a real incident of the same rule.
    """
    rule = next(r for r in cfg.rules if r.category == category)
    return Alert(
        f"synthetic:{rule.id}", category, rule.severity, f"[SYNTHETIC] {rule.summary}",
        rule.runbook, observed=0.0, threshold=rule.threshold, synthetic=True,
    )  # fmt: skip
