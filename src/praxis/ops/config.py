"""Versioned SLO / alert / dashboard configuration (``configs/ops/*.toml``)."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from praxis.telemetry import METRICS

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "configs" / "ops"
RUNBOOK_DIR = REPO_ROOT / "docs" / "runbooks"

Category = Literal["service", "data", "model", "business"]
Op = Literal[">", ">=", "<", "<=", "abs>"]


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Slo(_Cfg):
    id: str = Field(min_length=1)
    category: Category
    description: str
    kind: Literal["ratio", "latency", "gauge_max"]
    metric: str | None = None
    total_metric: str | None = None
    bad_metric: str | None = None
    good_label: str | None = None
    good_values: tuple[str, ...] = ()
    target: float | None = Field(default=None, gt=0, lt=1)
    quantile: float | None = Field(default=None, gt=0, lt=1)
    max_ms: float | None = Field(default=None, gt=0)
    max_value: float | None = None
    min_events: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        names: tuple[str | None, ...]
        if self.kind == "ratio":
            ok = (
                self.total_metric
                and self.target
                and bool(self.bad_metric) != bool(self.good_label and self.good_values)
            )
            need, names = ok, (self.total_metric, self.bad_metric)
        elif self.kind == "latency":
            need, names = self.metric and self.quantile and self.max_ms, (self.metric,)
        else:
            need, names = self.metric and self.max_value is not None, (self.metric,)
        if not need:
            raise ValueError(f"slo {self.id}: fields do not match kind {self.kind!r}")
        unknown = [n for n in names if n and n not in METRICS]
        if unknown:
            raise ValueError(f"slo {self.id}: unknown metric {unknown}")
        return self


class ThresholdBy(_Cfg):
    label: str
    values: dict[str, float]


class Rule(_Cfg):
    id: str = Field(min_length=1)
    category: Category
    severity: Literal["page", "ticket"]
    kind: Literal["slo", "threshold"]
    runbook: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    slo: str | None = None
    metric: str | None = None
    labels: dict[str, str] = {}
    denominator: str | None = None
    min_denominator: int = Field(default=1, ge=1)
    op: Op | None = None
    threshold: float | None = None
    threshold_by: ThresholdBy | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.kind == "slo":
            if not self.slo:
                raise ValueError(f"rule {self.id}: slo rules need `slo`")
            return self
        if not (self.metric and self.op) or (self.threshold is None) == (self.threshold_by is None):
            raise ValueError(f"rule {self.id}: threshold rules need metric, op and one threshold")
        if self.metric not in METRICS or (self.denominator and self.denominator not in METRICS):
            raise ValueError(f"rule {self.id}: unknown metric")
        return self


class Panel(_Cfg):
    section: Category
    title: str
    metric: str
    agg: Literal["total", "peak", "trough", "p50", "p95"]


class OpsConfig(_Cfg):
    slo_version: str
    alert_version: str
    dashboard_version: str
    slos: tuple[Slo, ...]
    rules: tuple[Rule, ...]
    panels: tuple[Panel, ...]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ids = [s.id for s in self.slos]
        rule_ids = [r.id for r in self.rules]
        if len(set(ids)) != len(ids) or len(set(rule_ids)) != len(rule_ids):
            raise ValueError("duplicate slo or rule id")
        missing = [r.id for r in self.rules if r.kind == "slo" and r.slo not in ids]
        if missing:
            raise ValueError(f"rules reference unknown slos: {missing}")
        if any(p.metric not in METRICS for p in self.panels):
            raise ValueError("panel references an unknown metric")
        return self

    def slo(self, slo_id: str) -> Slo:
        return next(s for s in self.slos if s.id == slo_id)


def required[T](value: T | None, what: str) -> T:
    """Narrow an optional config field that validation guarantees for this kind."""
    if value is None:
        raise ValueError(f"missing {what}")
    return value


def load_ops_config(directory: Path | None = None) -> OpsConfig:
    base = directory or CONFIG_DIR
    slos = tomllib.loads((base / "slos.toml").read_text())
    alerts = tomllib.loads((base / "alerts.toml").read_text())
    dash = tomllib.loads((base / "dashboard.toml").read_text())
    return OpsConfig(
        slo_version=slos["version"],
        alert_version=alerts["version"],
        dashboard_version=dash["version"],
        slos=tuple(Slo(**s) for s in slos["slo"]),
        rules=tuple(Rule(**r) for r in alerts["rule"]),
        panels=tuple(Panel(**p) for p in dash["panel"]),
    )
