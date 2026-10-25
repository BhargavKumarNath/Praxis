"""Runbook drill: docs/runbooks/agent-failure.md, executed step by step.

Fault: the LLM provider is down (no fallback configured for the drill).
Diagnose 1: the run's audit trail shows the outage and the failed run.
Diagnose 2: ``GET /v1/ops/health`` does not show the agent outage as degraded service.
Mitigate 1: disable the agent (``[agent] enabled = false``); operate through the CLIs.
Mitigate 2: a price only moves through ``pricing approve`` / ``execute`` with a named human.
Mitigate 3: a failed guarded call's audit record is inspected before anything is retried.
Verify: re-enabled with the deterministic fallback, a run completes and is grounded.
"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from praxis.agent.config import AgentPolicy
from praxis.agent.executor import ToolExecutor
from praxis.agent.operator import Operator
from praxis.agent.provider import DownProvider, Task
from praxis.agent.triage import TriageProvider
from praxis.agent.wiring import TOOLS
from praxis.api.app import create_app
from praxis.config import Settings
from praxis.ops.config import RUNBOOK_DIR
from praxis.ops.runbooks import alerts_of
from praxis.pricing.config import Mode
from tests.agent.conftest import World
from tests.agent.helpers import recorded_change

DAY = date(2026, 8, 20)


def _executor(world: World, policy: AgentPolicy) -> ToolExecutor:
    return ToolExecutor(TOOLS, world.backends, world.audit, policy)


def test_agent_failure_runbook_procedure(memory_world: World, policy: AgentPolicy) -> None:
    text = (RUNBOOK_DIR / "agent-failure.md").read_text()
    assert alerts_of(text) == []  # no alert rule: detection is the failed run + its audit

    # Trigger: provider outage
    run = Operator(_executor(memory_world, policy), DownProvider(), fallback=None).investigate(
        Task("daily_check", DAY)
    )
    assert run.outcome == "provider_unavailable"

    # Diagnose 1: the audit trail of the failed run
    session = memory_world.audit.events(session_id=run.session_id)
    assert [e.status for e in session] == [
        "started",
        "provider_unavailable",
        "provider_unavailable",
    ]
    assert session[1].detail == {"provider": "down", "error_type": "ProviderUnavailable"}

    # Diagnose 2: core health is not degraded by the agent outage
    health = (
        TestClient(create_app(Settings(), incident_store=memory_world.backends.incidents.store))
        .get("/v1/ops/health")
        .json()
    )
    assert health["status"] != "degraded"
    assert "agent" not in str({k: v["alerts"] for k, v in health["sections"].items()})

    # Mitigate 1: disable the agent; it then refuses to run
    off = policy.model_copy(update={"agent": policy.agent.model_copy(update={"enabled": False})})
    refused = Operator(_executor(memory_world, off), TriageProvider(policy.triage),
                       fallback=None).investigate(Task("daily_check", DAY))  # fmt: skip
    assert refused.outcome == "agent_disabled" and refused.calls == []

    # Mitigate 2: prices only move with an attributed human approval (never as the agent)
    decision = recorded_change(memory_world, Mode.RECOMMEND)
    store = memory_world.backends.decisions
    assert store is not None
    store.approve(decision.decision_id, "owner")
    done = store.execute(decision.decision_id, "owner")
    assert done.executed_by == "owner"

    # Mitigate 3: inspect a failed guarded call's audit record before any retry
    executor = _executor(memory_world, policy)
    s = executor.new_session()
    failed = executor.call(
        s, "request_rollback", {"reason": "x", "evidence": ["call_000000000000_0"]}
    )
    record = memory_world.audit.events(call_id=failed.call_id)
    assert [e.phase for e in record] == ["received", "outcome"]
    assert record[-1].status == "invalid" and "evidence" in record[-1].detail["error"]

    # Verify: re-enabled with the deterministic fallback, a run completes and is grounded
    ok = Operator(executor, DownProvider(), fallback=TriageProvider(policy.triage)).investigate(
        Task("daily_check", DAY)
    )
    assert ok.outcome == "completed" and ok.grounded and ok.fallback_used
