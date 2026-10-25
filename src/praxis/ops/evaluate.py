"""Pure evaluators: metric snapshot -> SLO statuses -> firing alerts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from praxis.ops.config import Category, Rule, Slo, required
from praxis.telemetry import MetricsView, Point

SloState = Literal["ok", "breach", "no_data"]


@dataclass(frozen=True)
class SloStatus:
    id: str
    category: Category
    description: str
    state: SloState
    target: str
    observed: float | None
    budget_consumed: float | None = None  # ratio SLOs: 1.0 = error budget fully spent

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "category": self.category, "description": self.description,
            "state": self.state, "target": self.target, "observed": self.observed,
            "budget_consumed": self.budget_consumed,
        }  # fmt: skip


@dataclass(frozen=True)
class Alert:
    rule_id: str
    category: Category
    severity: str
    summary: str
    runbook: str
    observed: float
    threshold: float | None
    series: dict[str, str] = field(default_factory=dict)
    synthetic: bool = False

    @property
    def fingerprint(self) -> str:
        return self.rule_id

    @property
    def runbook_path(self) -> str:
        return f"docs/runbooks/{self.runbook}.md"

    def context(self) -> dict[str, Any]:
        return {
            "observed": self.observed, "threshold": self.threshold, "series": self.series,
            "runbook": self.runbook_path, "synthetic": self.synthetic,
        }  # fmt: skip


def evaluate_slo(slo: Slo, view: MetricsView) -> SloStatus:
    if slo.kind == "ratio":
        return _ratio(slo, view)
    if slo.kind == "latency":
        return _latency(slo, view)
    return _gauge_max(slo, view)


def evaluate_slos(slos: tuple[Slo, ...], view: MetricsView) -> list[SloStatus]:
    return [evaluate_slo(s, view) for s in slos]


def _status(slo: Slo, state: SloState, target: str, observed: float | None, **kw: Any) -> SloStatus:
    return SloStatus(slo.id, slo.category, slo.description, state, target, observed, **kw)


def _ratio(slo: Slo, view: MetricsView) -> SloStatus:
    total_metric, goal = required(slo.total_metric, "total_metric"), required(slo.target, "target")
    target = f"good ratio >= {goal}"
    total = view.total(total_metric)
    if total < slo.min_events:
        return _status(slo, "no_data", target, None)
    if slo.bad_metric:
        bad = view.total(slo.bad_metric)
    else:
        good = sum(
            p.value
            for p in view.select(total_metric)
            if p.labels.get(slo.good_label or "") in slo.good_values
        )
        bad = total - good
    sli = 1.0 - bad / total
    budget = (bad / total) / (1.0 - goal)
    return _status(slo, "ok" if sli >= goal else "breach", target, sli, budget_consumed=budget)


def _latency(slo: Slo, view: MetricsView) -> SloStatus:
    metric, q = required(slo.metric, "metric"), required(slo.quantile, "quantile")
    limit = required(slo.max_ms, "max_ms")
    target = f"p{round(q * 100)} <= {limit} ms"
    observed = view.quantile(metric, q)
    if view.count(metric) < slo.min_events or observed is None:
        return _status(slo, "no_data", target, None)
    return _status(slo, "ok" if observed <= limit else "breach", target, observed)


def _gauge_max(slo: Slo, view: MetricsView) -> SloStatus:
    metric, limit = required(slo.metric, "metric"), required(slo.max_value, "max_value")
    target = f"<= {limit}"
    worst = view.peak(metric)
    if worst is None:
        return _status(slo, "no_data", target, None)
    return _status(slo, "ok" if worst <= limit else "breach", target, worst)


def _violates(op: str, value: float, threshold: float) -> bool:
    return {
        ">": value > threshold,
        ">=": value >= threshold,
        "<": value < threshold,
        "<=": value <= threshold,
        "abs>": abs(value) > threshold,
    }[op]


def _series(rule: Rule, view: MetricsView) -> list[tuple[dict[str, str], float]]:
    """The values a threshold rule judges: one ratio, or one value per matching series."""
    metric = required(rule.metric, "metric")
    if rule.denominator:
        den = view.total(rule.denominator)
        if den < rule.min_denominator:
            return []
        return [({}, view.total(metric, **rule.labels) / den)]
    points: list[Point] = view.select(metric, **rule.labels)
    return [(p.labels, p.value) for p in points]


def _threshold_for(rule: Rule, labels: dict[str, str]) -> float | None:
    if rule.threshold_by is None:
        return rule.threshold
    # A series with no configured limit is not judged (the config test pins the source list).
    return rule.threshold_by.values.get(labels.get(rule.threshold_by.label, ""))


def evaluate_alerts(
    rules: tuple[Rule, ...], slo_statuses: list[SloStatus], view: MetricsView
) -> list[Alert]:
    by_slo = {s.id: s for s in slo_statuses}
    firing: list[Alert] = []
    for rule in rules:
        if rule.kind == "slo":
            status = by_slo[rule.slo or ""]
            if status.state == "breach":
                firing.append(_alert(rule, status.observed or 0.0, None, {"slo": status.id}))
            continue
        op = required(rule.op, "op")
        for labels, value in _series(rule, view):
            limit = _threshold_for(rule, labels)
            if limit is not None and _violates(op, value, limit):
                firing.append(_alert(rule, value, limit, labels))
                break  # one alert per rule; the worst series is in the incident context
    return firing


def _alert(rule: Rule, observed: float, threshold: float | None, series: dict[str, str]) -> Alert:
    return Alert(
        rule.id, rule.category, rule.severity, rule.summary, rule.runbook,
        observed, threshold, series,
    )  # fmt: skip
