"""One evaluation tick: metrics -> health report -> alerts -> incidents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from praxis.ops.config import Category, OpsConfig
from praxis.ops.evaluate import Alert
from praxis.ops.health import HealthReport, build_health
from praxis.ops.incidents import IncidentManager, IncidentReport
from praxis.telemetry import telemetry


@dataclass(frozen=True)
class Tick:
    health: HealthReport
    incidents: IncidentReport


class OpsMonitor:
    def __init__(self, cfg: OpsConfig, manager: IncidentManager) -> None:
        self.cfg = cfg
        self.manager = manager

    def tick(self, now: datetime | None = None, *, extra: list[Alert] | None = None) -> Tick:
        now = now or datetime.now(UTC)
        health = build_health(self.cfg, telemetry().snapshot(), self.manager.store.live(), now=now)
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
