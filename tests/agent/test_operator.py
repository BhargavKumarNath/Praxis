"""Investigation loop: grounding, provider outage fallback, budgets, tool-error recovery."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from praxis.agent.audit import AuditEvent, MemoryAuditStore, incomplete_calls
from praxis.agent.config import AgentPolicy
from praxis.agent.executor import AuditUnavailable, ToolExecutor
from praxis.agent.operator import Operator
from praxis.agent.provider import (
    DownProvider,
    Final,
    LLMProvider,
    ProviderUnavailable,
    ScriptedProvider,
    Task,
    ToolCall,
)
from praxis.agent.triage import TriageProvider
from praxis.agent.wiring import TOOLS, UnknownProvider, operator_for, provider_for
from praxis.domain.model_lifecycle import ModelStage
from praxis.dunning.store import RecoveryWindow
from praxis.telemetry import Telemetry
from tests.agent.conftest import NOW, World
from tests.agent.helpers import CAND, CHAMP, champion_with_history, report, shadow_candidate

DAY = date(2026, 8, 20)


def _policy(policy: AgentPolicy, **agent: object) -> AgentPolicy:
    return policy.model_copy(update={"agent": policy.agent.model_copy(update=agent)})


def _operator(
    world: World, provider: LLMProvider, policy: AgentPolicy, fallback: bool = True
) -> Operator:
    executor = ToolExecutor(TOOLS, world.backends, world.audit, policy)
    return Operator(
        executor, provider, fallback=TriageProvider(policy.triage) if fallback else None
    )


def test_triage_completes_a_grounded_investigation(
    memory_world: World, policy: AgentPolicy, tel: Telemetry
) -> None:
    tel.set("data_source_age_s", 7_000_000, source="fred")
    run = _operator(memory_world, TriageProvider(policy.triage), policy).investigate(
        Task("daily_check", DAY)
    )
    assert run.outcome == "completed" and run.grounded and not run.fallback_used
    assert run.final is not None and run.final.diagnosis == "data_source_stale"
    assert run.final.recommended_action == "open_incident"
    tools = [c.tool for c in run.calls]
    assert tools[:2] == ["get_system_health", "get_source_freshness"]
    assert "get_runbook" in tools and tools[-1] == "open_incident"
    # recovery read model is absent in the memory world: an error, not a guessed number
    assert next(c for c in run.calls if c.tool == "inspect_recovery_metrics").status == "error"
    assert incomplete_calls(memory_world.audit.events(session_id=run.session_id)) == []
    assert run.as_dict()["notes"][0].startswith("All business numbers are SYNTHETIC")
    assert tel.snapshot().total("agent_investigations_total", outcome="completed") == 1


def test_no_issue_takes_no_action(memory_world: World, policy: AgentPolicy) -> None:
    run = _operator(memory_world, TriageProvider(policy.triage), policy).investigate(
        Task("daily_check", DAY)
    )
    assert run.final is not None and run.final.diagnosis == "no_issue"
    assert run.final.recommended_action is None
    assert all((c.tier or 0) == 0 for c in run.calls)


def test_ungrounded_answer_is_reported(memory_world: World, policy: AgentPolicy) -> None:
    provider = ScriptedProvider(
        [ToolCall("get_system_health", {}), Final("no_issue", "fine", ("call_000000000000_7",))]
    )
    run = _operator(memory_world, provider, policy).investigate(Task("daily_check", DAY))
    assert run.outcome == "completed" and run.grounded is False
    empty = ScriptedProvider([Final("no_issue", "fine", ())])
    assert (
        _operator(memory_world, empty, policy).investigate(Task("daily_check", DAY)).grounded
        is False
    )


def test_provider_outage_falls_back_to_triage(memory_world: World, policy: AgentPolicy) -> None:
    provider = ScriptedProvider([ToolCall("get_system_health", {}), ProviderUnavailable("503")])
    run = _operator(memory_world, provider, policy).investigate(Task("daily_check", DAY))
    assert run.outcome == "completed" and run.grounded
    assert run.providers == ["scripted", "deterministic-triage"] and run.fallback_used
    statuses = [e.status for e in memory_world.audit.events(session_id=run.session_id)
                if e.phase == "session"]  # fmt: skip
    assert statuses == ["started", "fallback", "completed"]


def test_outage_without_fallback_ends_honestly(memory_world: World, policy: AgentPolicy) -> None:
    run = _operator(memory_world, DownProvider(), policy, fallback=False).investigate(
        Task("daily_check", DAY)
    )
    assert run.outcome == "provider_unavailable" and run.final is None and not run.grounded
    assert "docs/runbooks/agent-failure.md" in run.as_dict()["notes"][-1]
    # the fallback itself failing also ends the run
    triage_down = Operator(
        ToolExecutor(TOOLS, memory_world.backends, memory_world.audit, policy),
        DownProvider(), fallback=None,
    )  # fmt: skip
    assert triage_down.investigate(Task("daily_check", DAY)).outcome == "provider_unavailable"


def test_fallback_that_also_fails(memory_world: World, policy: AgentPolicy) -> None:
    executor = ToolExecutor(TOOLS, memory_world.backends, memory_world.audit, policy)
    down = DownProvider()
    run = Operator(executor, down, fallback=down).investigate(Task("daily_check", DAY))
    assert run.outcome == "provider_unavailable"


def test_disabled_agent_does_nothing(memory_world: World, policy: AgentPolicy) -> None:
    off = _policy(policy, enabled=False)
    run = _operator(memory_world, TriageProvider(policy.triage), off).investigate(
        Task("daily_check", DAY)
    )
    assert run.outcome == "agent_disabled" and run.calls == [] and run.steps == 0
    assert [e.status for e in memory_world.audit.events()] == ["agent_disabled"]


def test_step_limit_and_tool_error_budget(memory_world: World, policy: AgentPolicy) -> None:
    loop = ScriptedProvider([ToolCall("get_system_health", {})] * 40)
    run = _operator(memory_world, loop, policy).investigate(Task("daily_check", DAY))
    assert run.outcome == "step_limit" and run.steps == policy.agent.max_steps
    bad = ScriptedProvider([ToolCall("run_shell", {"c": "x"})] * 10)
    run = _operator(memory_world, bad, policy).investigate(Task("daily_check", DAY))
    assert run.outcome == "tool_errors" and len(run.calls) == policy.agent.max_tool_errors


class FlakyRecovery:
    """Backend double: fails on the first window query, then answers."""

    def __init__(self) -> None:
        self.calls = 0

    def window(self, start: datetime, end: datetime) -> RecoveryWindow:
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("dunning replica restarting")
        return RecoveryWindow(start, end, 40, 30, 10, 0, 40_000, 30_000)


def test_triage_recovers_from_a_tool_error(memory_world: World, policy: AgentPolicy) -> None:
    memory_world.backends.recovery = FlakyRecovery()
    run = _operator(memory_world, TriageProvider(policy.triage), policy).investigate(
        Task("daily_check", DAY)
    )
    recovery = [c.status for c in run.calls if c.tool == "inspect_recovery_metrics"]
    assert recovery == ["error", "ok"] and run.outcome == "completed" and run.grounded
    assert run.final is not None and run.final.diagnosis == "no_issue"


def test_review_candidate_ready_and_regressed(pg_world: World, policy: AgentPolicy) -> None:
    reg = pg_world.backends.registry
    assert reg is not None
    champion_with_history(reg.store)
    shadow_candidate(reg.store, regressed=True)
    op = _operator(pg_world, TriageProvider(policy.triage), policy)
    task = Task("review_candidate", DAY, model_version=CAND)
    regressed = op.investigate(task)
    assert regressed.final is not None and regressed.final.diagnosis == "candidate_regression"
    assert regressed.final.recommended_action is None and pg_world.requests.recent() == []
    good = report(CAND, "shadow", False, {"champion": CHAMP})
    reg.store.add_monitoring_report(replace(good, as_of=good.as_of + timedelta(days=1)))
    ready = op.investigate(task)
    assert ready.final is not None and ready.final.diagnosis == "no_issue"
    assert ready.final.recommended_action == "request_promotion"
    assert [r.kind for r in pg_world.requests.recent()] == ["promotion"]
    champion = reg.store.one_in_stage(reg.model_name, ModelStage.CHAMPION)
    assert champion is not None and champion.model_version == CHAMP  # requested, not promoted


def test_review_without_registry_is_unknown(memory_world: World, policy: AgentPolicy) -> None:
    run = _operator(memory_world, TriageProvider(policy.triage), policy).investigate(
        Task("review_candidate", DAY, model_version=CAND)
    )
    assert run.final is not None and run.final.diagnosis == "unknown"


def test_outcome_audit_failure_is_surfaced(memory_world: World, policy: AgentPolicy) -> None:
    class HalfAudit(MemoryAuditStore):
        def append(self, event: AuditEvent) -> None:
            if event.phase == "outcome":
                raise ConnectionError("audit db lost")
            super().append(event)

    executor = ToolExecutor(TOOLS, memory_world.backends, HalfAudit(), policy)
    with pytest.raises(AuditUnavailable):
        executor.call(executor.new_session(), "get_system_health", {})


def test_wiring_providers(memory_world: World, policy: AgentPolicy) -> None:
    assert isinstance(provider_for("deterministic-triage", policy), TriageProvider)
    with pytest.raises(UnknownProvider, match="gemini"):
        provider_for("gemini", policy)
    executor = ToolExecutor(TOOLS, memory_world.backends, memory_world.audit, policy)
    op = operator_for(executor)
    assert op.fallback is not None and op.provider.name == "deterministic-triage"
    no_fallback = operator_for(
        ToolExecutor(
            TOOLS, memory_world.backends, memory_world.audit, _policy(policy, fallback=None)
        )
    )
    assert no_fallback.fallback is None
    assert NOW.tzinfo is not None
