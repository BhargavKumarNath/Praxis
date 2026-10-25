"""Tool contract: tiers, typed argument schemas, the backends tools read, provenance labels.

A tool is a name, a tier, a pydantic argument model (``extra="forbid"``) and a handler.
Handlers receive validated arguments only; the executor (``praxis.agent.executor``) owns
validation, permissions and audit, so a handler can be called and tested on its own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine

from praxis.agent.requests import RequestInbox
from praxis.control.db import transient_errors
from praxis.dunning.store import RecoveryWindow, recovery_window
from praxis.mlops.registry import ModelRegistry
from praxis.ops.config import RUNBOOK_DIR, Category, OpsConfig
from praxis.ops.incidents import IncidentManager
from praxis.ops.system import SystemSnapshot
from praxis.pricing.freeze import FreezeStore
from praxis.pricing.store import DecisionStore
from praxis.telemetry import telemetry


class Tier(IntEnum):
    READ = 0  # metrics, diagnosis, reports
    LOW_RISK = 1  # create incident, request retraining, collect diagnostics
    GUARDED = 2  # freeze pricing, rollback, promotion: pending request + named human approver


class ToolError(RuntimeError):
    """An expected tool failure (not found, not applicable); the message is safe to show."""


# ------------------------------------------------------------------ provenance labels
SYNTHETIC = "All business numbers are SYNTHETIC simulator output, never real-world results."
# How each Phase 11 signal is fed (ADR 0020). A metric with no value is reported with its
# feed and the live input status (collector / process), never as a healthy zero.
FEEDS = {
    "data_source_age_s": "pulled from the warehouse by the ops collectors (newest observation)",
    "data_contract_failures_total": (
        "pulled from the warehouse by the ops collectors (quarantined batches, last 24 h)"
    ),
    "business_kpi_deviation": (
        "pulled from the SYNTHETIC payment ledger by the revenue collector (needs 7 complete "
        "days of history)"
    ),
    "queue_backlog": (
        "published by consumers whose broker can count it (the in-memory harness); the Pub/Sub "
        "emulator exposes no backlog and Cloud Pub/Sub would need Cloud Monitoring"
    ),
    "dlq_backlog": "pulled from the control-plane dead_letters table by the ops collectors",
}
MONITOR_COMPONENT = "ops-monitor"


def scope_note(system: SystemSnapshot) -> str:
    """What these metrics cover, from the actual inputs of this read."""
    if system.scope != "fleet":
        return (
            f"Telemetry scope: {system.scope}. These are only the metrics of the process running "
            "the agent, not of the whole deployment."
        )
    stale = sorted({p.component for p in system.processes if p.stale})
    return (
        "Telemetry scope: fleet (every process that published within its TTL, plus the pull "
        "collectors; ADR 0020)." + (f" STALE and left out: {', '.join(stale)}." if stale else "")
    )


def monitor_note(system: SystemSnapshot) -> str:
    """Whether the ops monitor (the scheduler of OpsMonitor.tick) is running."""
    seen = [p for p in system.processes if p.component == MONITOR_COMPONENT]
    if any(not p.stale for p in seen):
        return "The ops monitor is running: incidents reflect its latest tick."
    state = "stale (stopped or stalled)" if seen else "not seen"
    return (
        f"The ops monitor is {state}: incidents may not reflect current alerts "
        "(start it with `python -m praxis.ops run`)."
    )


PRICING_NOT_VALIDATED = (
    "Pricing results here are SHADOW evaluations and are NOT validated for execution: the "
    "Phase 4 loader defect requires a fresh pre-registered Phase 6/9 evaluation before live "
    "pricing."
)
# Runbooks whose procedure is executed end to end by a test; the rest are structure-checked.
EXERCISED_RUNBOOKS = {
    "pubsub-backlog": "tests/ops/test_runbook_drill.py",
    "agent-failure": "tests/agent/test_runbook_agent_failure.py",
}

# ------------------------------------------------------------------ argument types
CallRef = Annotated[str, Field(pattern=r"^call_[0-9a-f]{12}_[0-9]{1,3}$")]
Evidence = Annotated[list[CallRef], Field(min_length=1, max_length=10)]
Text = Annotated[str, Field(min_length=1, max_length=300)]
ModelVersion = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,79}$")]
DecisionId = Annotated[str, Field(pattern=r"^dec-[0-9a-f]{20}$")]
IncidentId = Annotated[str, Field(pattern=r"^inc_[0-9a-f]{16}$")]
Scope = Annotated[str, Field(pattern=r"^(\*|[a-z][a-z0-9_]{0,39})$")]
Diagnosis = Literal[
    "data_source_stale", "demand_spike", "model_drift", "candidate_regression",
    "queue_backlog", "recovery_collapse", "price_volatility", "no_issue", "unknown",
]  # fmt: skip
RunbookName = Literal[
    "external-source-outage", "stripe-webhook-surge", "stale-model",
    "broken-candidate-deployment", "database-outage", "pubsub-backlog",
    "severe-forecast-error", "agent-failure", "business-kpi-shock",
]  # fmt: skip

# diagnosis -> (ops category, runbook) used when the agent opens an incident
ROUTES: dict[str, tuple[Category, str]] = {
    "data_source_stale": ("data", "external-source-outage"),
    "demand_spike": ("business", "business-kpi-shock"),
    "model_drift": ("model", "stale-model"),
    "candidate_regression": ("model", "broken-candidate-deployment"),
    "queue_backlog": ("service", "pubsub-backlog"),
    "recovery_collapse": ("business", "business-kpi-shock"),
    "price_volatility": ("business", "business-kpi-shock"),
    "unknown": ("service", "agent-failure"),
}


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NoArgs(Args):
    pass


# ------------------------------------------------------------------ backends
class RecoverySource(Protocol):
    def window(self, start: datetime, end: datetime) -> RecoveryWindow: ...


class PostgresRecoverySource:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def window(self, start: datetime, end: datetime) -> RecoveryWindow:
        with transient_errors(), self.engine.connect() as conn:
            return recovery_window(conn, start, end)


def _now() -> datetime:
    return datetime.now(UTC)


def _local_system() -> SystemSnapshot:
    return SystemSnapshot(telemetry().snapshot(), "process")


@dataclass
class Backends:
    """Everything a tool may read or request. Absent backends make tools report so."""

    ops: OpsConfig
    incidents: IncidentManager
    freezes: FreezeStore
    requests: RequestInbox
    registry: ModelRegistry | None = None
    decisions: DecisionStore | None = None
    recovery: RecoverySource | None = None
    products: frozenset[str] | None = None  # priced products (freeze scope check)
    system: Callable[[], SystemSnapshot] = _local_system  # ops.system.SystemReader in wiring
    runbook_dir: Path = RUNBOOK_DIR
    clock: Callable[[], datetime] = _now


@dataclass(frozen=True)
class SessionView:
    """Read-only facts about the running session that a handler may need."""

    session_id: str
    actor: str
    ok_calls: frozenset[str] = field(default_factory=frozenset)


Handler = Callable[[Backends, Any, SessionView], dict[str, Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    tier: Tier
    args: type[Args]
    handler: Handler
    summary: str

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tier": int(self.tier),
            "description": self.summary,
            "parameters": self.args.model_json_schema(),
        }


def evidence_of(args: Args) -> list[str]:
    value = getattr(args, "evidence", None)
    return list(value) if value else []
