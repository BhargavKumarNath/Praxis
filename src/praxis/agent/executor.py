"""The permission layer: every agent tool call goes through ``ToolExecutor.call``.

Order (each step fails closed):

1. audit ``received`` (keys + hash of the raw arguments only). If it cannot be written,
   nothing else happens;
2. the tool must be DECLARED: shell, SQL, secrets, prices, approvals are not tools;
3. arguments are validated against the tool's schema BEFORE anything runs;
4. the tier must be within the session's ceiling, which comes from ``configs/agent`` and is
   fixed when the session is created; no argument, task text or tool output can raise it;
5. Tier >= 1 calls consume the per-session action budget and must cite evidence: call ids of
   SUCCESSFUL earlier calls in the same session;
6. the handler runs; any failure becomes an ``error`` result (the investigation continues);
7. audit ``outcome`` with the validated arguments and the status.

Tool output is returned wrapped as untrusted data; the executor never interprets it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from praxis.agent.audit import AuditEvent, AuditStore, Phase
from praxis.agent.config import AgentPolicy
from praxis.agent.tools import Backends, SessionView, Tier, Tool, ToolError, evidence_of
from praxis.telemetry import telemetry
from praxis.tracing import current_trace_id

logger = logging.getLogger(__name__)

Status = Literal["ok", "refused", "invalid", "error", "audit_unavailable"]


class AuditUnavailable(RuntimeError):
    """The outcome of an executed call could not be audited (surfaced, never hidden)."""


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    tool: str
    tier: int | None
    status: Status
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def message(self) -> dict[str, Any]:
        """What the provider sees: tool output is DATA, never instructions."""
        return {
            "role": "tool", "call_id": self.call_id, "tool": self.tool, "status": self.status,
            "error": self.error, "untrusted_data": True, "data": self.data,
        }  # fmt: skip


@dataclass
class Session:
    session_id: str
    actor: str
    max_tier: Tier
    max_actions: int
    results: list[ToolResult] = field(default_factory=list)

    @property
    def actions_used(self) -> int:
        return sum(1 for r in self.results if r.ok and (r.tier or 0) >= Tier.LOW_RISK)

    def view(self) -> SessionView:
        return SessionView(
            self.session_id, self.actor, frozenset(r.call_id for r in self.results if r.ok)
        )


@dataclass(frozen=True)
class _Call:
    call_id: str
    tool: str
    tier: int | None

    def result(
        self, status: Status, *, data: dict[str, Any] | None = None, error: str | None = None
    ) -> ToolResult:
        return ToolResult(self.call_id, self.tool, self.tier, status, data or {}, error)


def _fingerprint(raw: Any) -> dict[str, Any]:
    keys = sorted(str(k) for k in raw) if isinstance(raw, Mapping) else []
    blob = json.dumps(raw, sort_keys=True, default=str)
    return {"keys": keys[:20], "sha256": hashlib.sha256(blob.encode()).hexdigest()}


def _validation_summary(error: ValidationError) -> str:
    # locations and error types only: input values (possibly hostile) are not echoed
    parts = [
        f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['type']}" for e in error.errors()
    ]
    return "; ".join(parts[:10])


class ToolExecutor:
    def __init__(
        self, tools: Iterable[Tool], backends: Backends, audit: AuditStore, policy: AgentPolicy
    ) -> None:
        self.tools = {t.name: t for t in tools}
        self.backends = backends
        self.audit = audit
        self.policy = policy

    def new_session(self) -> Session:
        a = self.policy.agent
        return Session(f"ses_{uuid.uuid4().hex[:12]}", a.actor, Tier(a.max_tier), a.max_actions)

    def session_event(self, session: Session, status: str, detail: dict[str, Any]) -> None:
        """Audit a session-level event (start, provider outage, fallback, end)."""
        call = _Call(session.session_id, "session", None)
        self._audit(session, call, "session", status, {}, detail)

    def catalogue(self, session: Session) -> list[dict[str, Any]]:
        """The tools this session may call (what a provider is shown)."""
        return [t.schema() for t in self.tools.values() if t.tier <= session.max_tier]

    def call(self, session: Session, name: str, raw_args: Any) -> ToolResult:
        tool = self.tools.get(name)
        call = _Call(
            f"call_{session.session_id[-12:]}_{len(session.results)}",
            name if tool else "undeclared",
            int(tool.tier) if tool else None,
        )
        try:
            self._audit(session, call, "received", "received", _fingerprint(raw_args))
        except Exception:  # fail closed: an unaudited call never runs
            logger.exception("agent.audit_unavailable", extra={"call_id": call.call_id})
            return self._finish(session, call.result("audit_unavailable", error="audit down"))
        result, args_record = self._run(session, call, name, tool, raw_args)
        detail = {"error": result.error} if result.error else {}
        try:
            self._audit(session, call, "outcome", result.status, args_record, detail)
        except Exception as error:
            logger.exception("agent.audit_outcome_failed", extra={"call_id": call.call_id})
            raise AuditUnavailable(f"outcome of {call.call_id} could not be audited") from error
        return self._finish(session, result)

    def _run(
        self, session: Session, call: _Call, name: str, tool: Tool | None, raw: Any
    ) -> tuple[ToolResult, dict[str, Any]]:
        if tool is None:
            refused = call.result("refused", error=f"'{name[:40]}' is not a declared tool")
            return refused, _fingerprint(raw)
        try:
            args = tool.args.model_validate(raw if isinstance(raw, Mapping) else {"<raw>": raw})
        except ValidationError as error:
            return call.result("invalid", error=_validation_summary(error)), _fingerprint(raw)
        record = args.model_dump(mode="json")
        refusal = self._permission(session, tool, evidence_of(args))
        if refusal is not None:
            status: Status = "invalid" if refusal.startswith("evidence") else "refused"
            return call.result(status, error=refusal), record
        try:
            data = tool.handler(self.backends, args, session.view())
        except ToolError as error:
            return call.result("error", error=str(error)), record
        except Exception as error:  # boundary: a failing backend is reported, audited, survived
            logger.exception(
                "agent.tool_failed", extra={"tool": tool.name, "call_id": call.call_id}
            )
            return call.result("error", error=f"{type(error).__name__} in {tool.name}"), record
        return call.result("ok", data=data), record

    def _permission(self, session: Session, tool: Tool, evidence: list[str]) -> str | None:
        if tool.tier > session.max_tier:
            return f"tier {int(tool.tier)} exceeds this session's ceiling {int(session.max_tier)}"
        if tool.tier >= Tier.LOW_RISK:
            if session.actions_used >= session.max_actions:
                return f"action budget exhausted ({session.max_actions} per investigation)"
            unknown = sorted(set(evidence) - session.view().ok_calls)
            if unknown:
                return f"evidence must cite successful calls of this session: {unknown}"
        return None

    def _audit(
        self,
        session: Session,
        call: _Call,
        phase: Phase,
        status: str,
        args: dict[str, Any],
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.audit.append(
            AuditEvent(
                call.call_id,
                session.session_id,
                session.actor,
                call.tool,
                call.tier,
                phase,
                status,
                args,
                self.backends.clock(),
                detail or {},
                current_trace_id(),
            )
        )

    def _finish(self, session: Session, result: ToolResult) -> ToolResult:
        session.results.append(result)
        telemetry().inc(
            "agent_tool_calls_total", tool=result.tool, tier=str(result.tier), status=result.status
        )
        return result
