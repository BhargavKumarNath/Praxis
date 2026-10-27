"""The key operational journey, driven through the REAL code paths (shared by the API contract
tests and the Playwright end-to-end seed):

1. a pricing process publishes its metrics (as ``python -m praxis.pricing decide`` does) and
   more than half of the products changed price in the cycle: ``price_change_fraction`` 0.8
   (alert limit 0.50, ``configs/ops/alerts.toml``);
2. the ops monitor ticks over the fleet view and opens the ``price_volatility`` incident;
3. the AI operator (deterministic triage, not an LLM) investigates that incident through the
   real permission layer and requests a price freeze: a PENDING request, nothing frozen.

The approval itself is what the console test / E2E test then performs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine

from praxis.agent.config import load_agent_policy
from praxis.agent.executor import ToolExecutor
from praxis.agent.provider import Task
from praxis.agent.wiring import TOOLS, control_plane, operator_for
from praxis.config import Environment
from praxis.control.metric_snapshots import MetricSnapshotStore
from praxis.ops.config import load_ops_config
from praxis.ops.incidents import IncidentManager, LogNotifier, PostgresIncidentStore
from praxis.ops.monitor import OpsMonitor
from praxis.ops.system import build_reader
from praxis.telemetry import Telemetry

PRICING_TTL = timedelta(hours=26)  # the pricing batch job's snapshot TTL (ADR 0020)


@dataclass(frozen=True)
class Journey:
    incident_id: str
    session_id: str
    request_id: str


def publish_price_volatility(engine: Engine, now: datetime, fraction: float = 0.8) -> None:
    tel = Telemetry()
    tel.set("price_change_fraction", fraction)
    MetricSnapshotStore(engine).publish(
        "pricing", "pricing-journey", tel.snapshot(), now=now, ttl=PRICING_TTL, started_at=now
    )


def run_journey(engine: Engine) -> Journey:
    now = datetime.now(UTC)
    publish_price_volatility(engine, now)
    manager = IncidentManager(PostgresIncidentStore(engine), LogNotifier())
    OpsMonitor(load_ops_config(), manager, build_reader(engine)).tick(now)
    incident = next(i for i in manager.store.live() if i.rule_id == "price_volatility")
    plane = control_plane(engine, Environment.LOCAL)
    executor = ToolExecutor(TOOLS, plane.backends, plane.audit, load_agent_policy())
    run = operator_for(executor).investigate(
        Task("investigate_incident", now.date(), incident_id=incident.incident_id)
    )
    pending = [
        r
        for r in plane.desk.store.recent(10)
        if r.session_id == run.session_id and r.status == "pending"
    ]
    assert run.final is not None and run.final.diagnosis == "price_volatility", run.outcome
    assert len(pending) == 1 and pending[0].kind == "freeze_pricing"
    return Journey(incident.incident_id, run.session_id, pending[0].request_id)
