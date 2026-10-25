"""Health report: service, data, model and business sections from one metric snapshot.

The same report backs ``GET /v1/ops/health`` (JSON) and ``GET /ops/dashboard`` (static
HTML). A section is ``degraded`` when one of its alerts fires, ``no_data`` when none of its
panels has a value yet, otherwise ``ok``. Synthetic simulator numbers are labelled as such
by the caller (``environment`` / ``data_origin``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from typing import Any

from praxis.ops.config import OpsConfig, Panel
from praxis.ops.evaluate import Alert, SloStatus, evaluate_alerts, evaluate_slos
from praxis.ops.incidents import Incident
from praxis.telemetry import MetricsView

SECTIONS = ("service", "data", "model", "business")


@dataclass(frozen=True)
class PanelValue:
    title: str
    metric: str
    agg: str
    value: float | None


@dataclass(frozen=True)
class Section:
    name: str
    status: str
    panels: tuple[PanelValue, ...]
    slos: tuple[SloStatus, ...]
    alerts: tuple[Alert, ...]


@dataclass(frozen=True)
class HealthReport:
    generated_at: datetime
    config_versions: dict[str, str]
    sections: tuple[Section, ...]
    incidents: tuple[Incident, ...]
    data_origin: str
    # Where the numbers came from (``ops.system.SystemSnapshot.inputs``); empty = this
    # process's metrics only.
    inputs: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        states = {s.status for s in self.sections}
        return (
            "degraded" if "degraded" in states else ("no_data" if states == {"no_data"} else "ok")
        )

    def alerts(self) -> list[Alert]:
        return [a for s in self.sections for a in s.alerts]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "generated_at": self.generated_at.isoformat(),
            "data_origin": self.data_origin,
            "config_versions": self.config_versions,
            "sections": {
                s.name: {
                    "status": s.status,
                    "panels": [p.__dict__ for p in s.panels],
                    "slos": [x.as_dict() for x in s.slos],
                    "alerts": [
                        {"rule_id": a.rule_id, "severity": a.severity, "summary": a.summary,
                         "runbook": a.runbook_path, **a.context()}
                        for a in s.alerts
                    ],
                }
                for s in self.sections
            },
            "incidents": [i.as_dict() for i in self.incidents],
            "inputs": self.inputs or {"telemetry_scope": "process"},
        }  # fmt: skip


def _panel_value(panel: Panel, view: MetricsView) -> float | None:
    if panel.agg == "total":
        return view.total(panel.metric) if view.select(panel.metric) else None
    if panel.agg == "peak":
        return view.peak(panel.metric)
    if panel.agg == "trough":
        return view.trough(panel.metric)
    return view.quantile(panel.metric, 0.5 if panel.agg == "p50" else 0.95)


def build_health(
    cfg: OpsConfig,
    view: MetricsView,
    incidents: list[Incident] | None = None,
    *,
    now: datetime | None = None,
    data_origin: str = "synthetic",
    inputs: dict[str, Any] | None = None,
) -> HealthReport:
    slo_statuses = evaluate_slos(cfg.slos, view)
    alerts = evaluate_alerts(cfg.rules, slo_statuses, view)
    sections = []
    for name in SECTIONS:
        panels = tuple(
            PanelValue(p.title, p.metric, p.agg, _panel_value(p, view))
            for p in cfg.panels
            if p.section == name
        )
        slos = tuple(s for s in slo_statuses if s.category == name)
        mine = tuple(a for a in alerts if a.category == name)
        if mine:
            status = "degraded"
        elif all(p.value is None for p in panels) and all(s.state == "no_data" for s in slos):
            status = "no_data"
        else:
            status = "ok"
        sections.append(Section(name, status, panels, slos, mine))
    return HealthReport(
        now or datetime.now(UTC),
        {"slo": cfg.slo_version, "alerts": cfg.alert_version, "dashboard": cfg.dashboard_version},
        tuple(sections),
        tuple(incidents or ()),
        data_origin,
        inputs or {},
    )


def _badge(status: str) -> str:
    return f"<span class={escape(status)}>{escape(status)}</span>"


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def render_html(report: HealthReport) -> str:
    """Self-contained static page; every dynamic value is HTML-escaped."""
    parts = [
        "<!doctype html><meta charset=utf-8><title>Praxis health</title>",
        "<style>body{font:14px system-ui;margin:2rem;max-width:60rem}"
        "h2{margin-top:1.5rem}.ok{color:#0a7d32}.degraded{color:#b00020}.no_data{color:#666}"
        "table{border-collapse:collapse}td,th{padding:.2rem .8rem;border-bottom:1px solid #ddd}"
        "</style>",
        f"<h1>Praxis health: {_badge(report.status)}</h1>",
        f"<p>Generated {escape(report.generated_at.isoformat())}. Data origin: "
        f"<b>{escape(report.data_origin)}</b>.</p>",
    ]
    for s in report.sections:
        parts.append(
            f"<h2>{escape(s.name)} <span class={escape(s.status)}>{escape(s.status)}</span></h2>"
        )
        parts.append("<table>")
        parts += [f"<tr><td>{escape(p.title)}</td><td>{_num(p.value)}</td></tr>" for p in s.panels]
        parts += [
            f"<tr><td>SLO {escape(x.id)}</td><td class={escape(x.state)}>{escape(x.state)} "
            f"({escape(x.target)})</td></tr>"
            for x in s.slos
        ]
        parts.append("</table>")
        parts += [
            f"<p class=degraded>ALERT {escape(a.rule_id)} [{escape(a.severity)}]: "
            f"{escape(a.summary)} (observed {a.observed:g}) runbook: {escape(a.runbook_path)}</p>"
            for a in s.alerts
        ]
    parts += _inputs_html(report.inputs)
    parts.append("<h2>Incidents</h2><table>")
    parts += [
        f"<tr><td>{escape(i.incident_id)}</td><td>{escape(i.status.value)}</td>"
        f"<td>{escape(i.rule_id)}</td><td>{escape(i.runbook)}</td></tr>"
        for i in report.incidents
    ] or ["<tr><td>none</td></tr>"]
    parts.append("</table>")
    return "".join(parts)


def _inputs_html(inputs: dict[str, Any]) -> list[str]:
    """Where the numbers came from; stale processes and unavailable collectors are flagged."""
    scope = str(inputs.get("telemetry_scope", "process"))
    parts = [f"<h2>Inputs</h2><p>Telemetry scope: <b>{escape(scope)}</b></p><table>"]
    for p in inputs.get("processes", []):
        state = "degraded" if p["stale"] else "ok"
        label = "STALE" if p["stale"] else "fresh"
        parts.append(
            f"<tr><td>{escape(p['component'])}</td><td>{escape(p['instance'])}</td>"
            f"<td class={state}>{label}, {p['age_s']:g}s old</td></tr>"
        )
    for name, note in inputs.get("collectors", {}).items():
        state = "ok" if note.get("status") == "ok" else "degraded"
        parts.append(
            f"<tr><td>collector {escape(name)}</td><td></td>"
            f"<td class={state}>{escape(str(note.get('status')))}</td></tr>"
        )
    parts.append("</table>")
    return parts
