"""The investigation loop: provider proposes, executor disposes, the answer must be grounded.

* The agent can be disabled by policy; it then refuses to run (and nothing else changes:
  pricing, billing, recovery and MLOps never import ``praxis.agent``).
* ``ProviderUnavailable`` switches to the fallback provider (deterministic triage) for the
  rest of the run; without a fallback the run ends ``provider_unavailable`` and points at
  the agent-failure runbook. Both are audited session events.
* A ``Final`` answer is grounded only if it cites at least one call id and every cited id is
  a SUCCESSFUL call of this session; an ungrounded answer is reported as such, never hidden.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from praxis.agent.executor import Session, ToolExecutor, ToolResult
from praxis.agent.provider import (
    Conversation,
    Final,
    LLMProvider,
    ProviderUnavailable,
    Task,
)
from praxis.agent.tools import SYNTHETIC
from praxis.telemetry import telemetry
from praxis.tracing import current_trace_id, trace_context

Outcome = Literal[
    "completed", "agent_disabled", "provider_unavailable", "step_limit", "tool_errors"
]
RUNBOOK = "docs/runbooks/agent-failure.md"


@dataclass
class Investigation:
    session_id: str
    task: Task
    outcome: Outcome
    providers: list[str]
    calls: list[ToolResult] = field(default_factory=list)
    final: Final | None = None
    grounded: bool = False
    steps: int = 0
    latency_ms: float = 0.0

    @property
    def fallback_used(self) -> bool:
        return len(self.providers) > 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id, "outcome": self.outcome, "providers": self.providers,
            "fallback_used": self.fallback_used, "steps": self.steps,
            "latency_ms": round(self.latency_ms, 3), "grounded": self.grounded,
            "calls": [{"call_id": c.call_id, "tool": c.tool, "tier": c.tier, "status": c.status,
                       "error": c.error} for c in self.calls],
            "final": None if self.final is None else self.final.__dict__,
            "notes": [SYNTHETIC] + ([f"see {RUNBOOK}"] if self.outcome != "completed" else []),
        }  # fmt: skip


def grounded(final: Final, session: Session) -> bool:
    ok = session.view().ok_calls
    return bool(final.evidence) and set(final.evidence) <= ok


class Operator:
    def __init__(
        self, executor: ToolExecutor, provider: LLMProvider, *, fallback: LLMProvider | None
    ) -> None:
        self.executor = executor
        self.provider = provider
        self.fallback = fallback

    def investigate(self, task: Task) -> Investigation:
        if current_trace_id() is not None:
            return self._investigate(task)
        with trace_context():
            return self._investigate(task)

    def _investigate(self, task: Task) -> Investigation:
        started = time.perf_counter()
        policy = self.executor.policy.agent
        session = self.executor.new_session()
        run = Investigation(session.session_id, task, "completed", [self.provider.name])
        if not policy.enabled:
            run.outcome = "agent_disabled"
            self.executor.session_event(session, "agent_disabled", {"task": task.kind})
            return self._done(run, started)
        self.executor.session_event(
            session, "started", {"task": task.kind, "provider": self.provider.name}
        )
        conv = Conversation(task, self.executor.catalogue(session), messages=[task.message()])
        self._loop(run, session, conv, policy.max_steps, policy.max_tool_errors)
        run.calls = list(session.results)
        if run.final is not None:
            run.grounded = grounded(run.final, session)
        self.executor.session_event(
            session, run.outcome,
            {"grounded": run.grounded, "steps": run.steps,
             "diagnosis": run.final.diagnosis if run.final else None},
        )  # fmt: skip
        return self._done(run, started)

    def _loop(
        self, run: Investigation, session: Session, conv: Conversation, max_steps: int,
        max_errors: int,
    ) -> None:  # fmt: skip
        provider = self.provider
        errors = 0
        while run.steps < max_steps:
            run.steps += 1
            try:
                step = provider.next_step(conv)
            except ProviderUnavailable as down:
                switched = self._switch(run, session, provider, down)
                if switched is None:
                    run.outcome = "provider_unavailable"
                    return
                provider = switched
                continue
            if isinstance(step, Final):
                run.final = step
                return
            result = self.executor.call(session, step.tool, step.args)
            conv.messages.append(result.message())
            errors += 0 if result.ok else 1
            if errors >= max_errors:
                run.outcome = "tool_errors"
                return
        run.outcome = "step_limit"

    def _switch(
        self, run: Investigation, session: Session, current: LLMProvider, down: Exception
    ) -> LLMProvider | None:
        detail = {"provider": current.name, "error_type": type(down).__name__}
        if self.fallback is None or current is self.fallback:
            self.executor.session_event(session, "provider_unavailable", detail)
            return None
        self.executor.session_event(session, "fallback", {**detail, "to": self.fallback.name})
        run.providers.append(self.fallback.name)
        return self.fallback

    @staticmethod
    def _done(run: Investigation, started: float) -> Investigation:
        run.latency_ms = (time.perf_counter() - started) * 1000.0
        telemetry().inc(
            "agent_investigations_total", provider=run.providers[-1], outcome=run.outcome
        )
        return run
