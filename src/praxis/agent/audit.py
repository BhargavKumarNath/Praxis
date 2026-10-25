"""Append-only audit of every agent tool call and session event (migration 0007).

Each call writes ``received`` BEFORE validation, permission checks or execution, and
``outcome`` afterwards, whatever happened (ok, refused, invalid, error). A call whose
``received`` row cannot be written is never executed (fail closed). Raw arguments of
undeclared or invalid calls are not stored, only their keys and a hash: hostile input never
lands verbatim in the audit trail.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from sqlalchemy import Engine, text

from praxis.control.db import transient_errors

Phase = Literal["received", "outcome", "session"]


@dataclass(frozen=True)
class AuditEvent:
    call_id: str
    session_id: str
    actor: str
    tool: str
    tier: int | None
    phase: Phase
    status: str
    args: dict[str, Any]
    at: datetime
    detail: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "at": self.at.isoformat()}


class AuditStore(Protocol):
    def append(self, event: AuditEvent) -> None: ...
    def events(
        self, *, session_id: str | None = None, call_id: str | None = None
    ) -> list[AuditEvent]: ...


class MemoryAuditStore:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = threading.Lock()

    def append(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def events(
        self, *, session_id: str | None = None, call_id: str | None = None
    ) -> list[AuditEvent]:
        with self._lock:
            return [
                e
                for e in self._events
                if session_id in (None, e.session_id) and call_id in (None, e.call_id)
            ]


_COLS = "call_id, session_id, actor, tool, tier, phase, status, args, at, detail, trace_id"


class PostgresAuditStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def append(self, event: AuditEvent) -> None:
        with transient_errors(), self.engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO agent_audit_events ({_COLS}) VALUES (:call_id, :session_id, "  # noqa: S608
                    ":actor, :tool, :tier, :phase, :status, CAST(:args AS JSONB), :at, "
                    "CAST(:detail AS JSONB), :trace_id)"
                ),
                {
                    **event.__dict__,
                    "args": json.dumps(event.args, default=str),
                    "detail": json.dumps(event.detail, default=str),
                },
            )

    def events(
        self, *, session_id: str | None = None, call_id: str | None = None
    ) -> list[AuditEvent]:
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT {_COLS} FROM agent_audit_events "  # noqa: S608 - constant columns
                    "WHERE (CAST(:s AS TEXT) IS NULL OR session_id = :s) "
                    "AND (CAST(:c AS TEXT) IS NULL OR call_id = :c) ORDER BY event_id"
                ),
                {"s": session_id, "c": call_id},
            ).all()
        return [AuditEvent(*r) for r in rows]


def incomplete_calls(events: list[AuditEvent]) -> list[str]:
    """Call ids that lack a ``received`` or an ``outcome`` row (empty = audit complete)."""
    phases: dict[str, set[str]] = {}
    for e in events:
        if e.phase != "session":
            phases.setdefault(e.call_id, set()).add(e.phase)
    return sorted(c for c, p in phases.items() if p != {"received", "outcome"})
