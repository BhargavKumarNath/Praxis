"""Guarded (Tier 2) action requests and the human approval desk.

The agent can only CREATE a pending request (``RequestInbox``). Completing one is
``ApprovalDesk.approve``: a named human, never the requester, runs it through the EXISTING
approval paths, which re-check their own policy:

* ``promotion`` -> ``ModelRegistry.promote(approver=...)`` (re-checks shadow readiness);
* ``rollback``  -> ``ModelRegistry.rollback(actor=...)``;
* ``freeze_pricing`` -> ``FreezeStore.activate(approved_by=...)``.

A request is claimed (pending -> executing) atomically before it runs, so two approvers
cannot both complete it; a refusal by the underlying path marks it ``failed`` with the
reason. The agent's tool context never holds an ``ApprovalDesk``.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from sqlalchemy import Engine, exc, text

from praxis.control.db import transient_errors
from praxis.domain.model_lifecycle import ModelStage
from praxis.mlops.registry import ModelRegistry
from praxis.mlops.store import RegistryError
from praxis.pricing.freeze import FreezeError, FreezeStore

Kind = Literal["freeze_pricing", "rollback", "promotion"]
Status = Literal["pending", "executing", "completed", "failed", "rejected"]


class RequestError(RuntimeError):
    """An illegal request action (unknown request, already decided, self-approval, ...)."""


@dataclass(frozen=True)
class ActionRequest:
    request_id: str
    kind: Kind
    target: str
    params: dict[str, Any]
    rationale: str
    evidence: list[str]
    requested_by: str
    session_id: str
    created_at: datetime
    status: Status = "pending"
    decided_by: str | None = None
    decided_at: datetime | None = None
    outcome: dict[str, Any] | None = field(default=None)

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "created_at": self.created_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }


def request_id(kind: str, target: str, created_at: datetime) -> str:
    raw = f"{kind}|{target}|{created_at.isoformat()}"
    return "req_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


class RequestInbox(Protocol):
    """What the agent's tools may do with requests: create and read, nothing else."""

    def create(self, request: ActionRequest) -> tuple[ActionRequest, bool]:
        """Insert, or return the pending request of the same (kind, target). (req, created)"""

    def get(self, request_id_: str) -> ActionRequest | None: ...
    def recent(self, limit: int = 50) -> list[ActionRequest]: ...


class RequestStore(RequestInbox, Protocol):
    def claim(self, request_id_: str, by: str, now: datetime) -> ActionRequest:
        """pending -> executing, atomically; RequestError if not pending or self-approval."""

    def finish(
        self, request_id_: str, status: Status, outcome: dict[str, Any], *, by: str, now: datetime
    ) -> ActionRequest: ...


def _check_decider(current: ActionRequest | None, request_id_: str, by: str) -> ActionRequest:
    if not by.strip():
        raise RequestError("the approver must be named")
    if current is None:
        raise RequestError(f"unknown request {request_id_}")
    if current.status != "pending":
        raise RequestError(f"request {request_id_} is {current.status}, not pending")
    if by == current.requested_by:
        raise RequestError("a request cannot be decided by its requester")
    return current


class MemoryRequestStore:
    def __init__(self) -> None:
        self._requests: dict[str, ActionRequest] = {}
        self._lock = threading.Lock()

    def create(self, request: ActionRequest) -> tuple[ActionRequest, bool]:
        with self._lock:
            for r in self._requests.values():
                if r.status == "pending" and (r.kind, r.target) == (request.kind, request.target):
                    return r, False
            self._requests[request.request_id] = request
            return request, True

    def get(self, request_id_: str) -> ActionRequest | None:
        with self._lock:
            return self._requests.get(request_id_)

    def recent(self, limit: int = 50) -> list[ActionRequest]:
        with self._lock:
            ordered = sorted(self._requests.values(), key=lambda r: r.created_at, reverse=True)
        return ordered[:limit]

    def claim(self, request_id_: str, by: str, now: datetime) -> ActionRequest:
        with self._lock:
            current = _check_decider(self._requests.get(request_id_), request_id_, by)
            claimed = replace(current, status="executing", decided_by=by, decided_at=now)
            self._requests[request_id_] = claimed
            return claimed

    def finish(
        self, request_id_: str, status: Status, outcome: dict[str, Any], *, by: str, now: datetime
    ) -> ActionRequest:
        with self._lock:
            current = self._requests[request_id_]
            done = replace(current, status=status, outcome=outcome, decided_by=by, decided_at=now)
            self._requests[request_id_] = done
            return done


_COLS = (
    "request_id, kind, target, params, rationale, evidence, requested_by, session_id, "
    "created_at, status, decided_by, decided_at, outcome"
)


class PostgresRequestStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create(self, request: ActionRequest) -> tuple[ActionRequest, bool]:
        params = {
            **request.__dict__,
            "params": json.dumps(request.params, default=str),
            "evidence": json.dumps(request.evidence),
            "outcome": None,
        }
        try:
            with transient_errors(), self.engine.begin() as conn:
                conn.execute(
                    text(
                        f"INSERT INTO agent_action_requests ({_COLS}) VALUES (:request_id, "  # noqa: S608
                        ":kind, :target, CAST(:params AS JSONB), :rationale, "
                        "CAST(:evidence AS JSONB), :requested_by, :session_id, :created_at, "
                        ":status, :decided_by, :decided_at, CAST(:outcome AS JSONB))"
                    ),
                    params,
                )
        except exc.IntegrityError:  # the one-pending-per-(kind, target) index
            pending = self._query(
                "WHERE kind = :k AND target = :t AND status = 'pending'",
                {"k": request.kind, "t": request.target},
            )
            if pending:
                return pending[0], False
            raise
        return request, True

    def get(self, request_id_: str) -> ActionRequest | None:
        rows = self._query("WHERE request_id = :i", {"i": request_id_})
        return rows[0] if rows else None

    def recent(self, limit: int = 50) -> list[ActionRequest]:
        return self._query("ORDER BY created_at DESC, request_id LIMIT :n", {"n": limit})

    def _query(self, tail: str, params: Mapping[str, Any]) -> list[ActionRequest]:
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT {_COLS} FROM agent_action_requests {tail}"),  # noqa: S608
                dict(params),
            ).all()
        return [ActionRequest(*r) for r in rows]

    def claim(self, request_id_: str, by: str, now: datetime) -> ActionRequest:
        with transient_errors(), self.engine.begin() as conn:
            row = conn.execute(
                text(f"SELECT {_COLS} FROM agent_action_requests WHERE request_id = :i FOR UPDATE"),  # noqa: S608
                {"i": request_id_},
            ).first()
            current = _check_decider(ActionRequest(*row) if row else None, request_id_, by)
            conn.execute(
                text(
                    "UPDATE agent_action_requests SET status = 'executing', decided_by = :b, "
                    "decided_at = :t WHERE request_id = :i"
                ),
                {"b": by, "t": now, "i": request_id_},
            )
        return replace(current, status="executing", decided_by=by, decided_at=now)

    def finish(
        self, request_id_: str, status: Status, outcome: dict[str, Any], *, by: str, now: datetime
    ) -> ActionRequest:
        with transient_errors(), self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE agent_action_requests SET status = :s, outcome = CAST(:o AS JSONB), "
                    "decided_by = :b, decided_at = :t WHERE request_id = :i"
                ),
                {"s": status, "o": json.dumps(outcome, default=str), "b": by, "t": now,
                 "i": request_id_},
            )  # fmt: skip
        done = self.get(request_id_)
        assert done is not None  # noqa: S101 - claimed above, rows are never deleted
        return done


class ApprovalDesk:
    """Human-only completion of guarded requests through the existing approval paths."""

    def __init__(
        self, store: RequestStore, *, registry: ModelRegistry | None, freezes: FreezeStore
    ) -> None:
        self.store = store
        self.registry = registry
        self.freezes = freezes

    def approve(
        self, request_id_: str, approver: str, now: datetime | None = None
    ) -> ActionRequest:
        now = now or datetime.now(UTC)
        req = self.store.claim(request_id_, approver, now)
        try:
            outcome = self._complete(req, approver)
        except (RegistryError, FreezeError, RequestError) as refused:
            return self.store.finish(
                req.request_id, "failed",
                {"refused_by_policy": type(refused).__name__, "reason": str(refused)},
                by=approver, now=now,
            )  # fmt: skip
        return self.store.finish(req.request_id, "completed", outcome, by=approver, now=now)

    def reject(
        self, request_id_: str, approver: str, reason: str, now: datetime | None = None
    ) -> ActionRequest:
        now = now or datetime.now(UTC)
        req = self.store.claim(request_id_, approver, now)
        return self.store.finish(
            req.request_id, "rejected", {"reason": reason}, by=approver, now=now
        )

    def _complete(self, req: ActionRequest, approver: str) -> dict[str, Any]:
        reason = f"approved agent request {req.request_id}: {req.rationale}"
        if req.kind == "freeze_pricing":
            frozen = self.freezes.activate(
                req.target, reason, requested_by=req.requested_by, approved_by=approver
            )
            return {"freeze": frozen.as_dict()}
        if self.registry is None:
            raise RequestError("no model registry is configured for this desk")
        if req.kind == "rollback":
            if req.params.get("champion_version") != self._champion():
                raise RequestError("the champion changed since the rollback was requested")
            decision = self.registry.rollback(actor=approver, reason=reason)
        else:
            decision = self.registry.promote(req.target, approver=approver, reason=reason)
        return {
            "registry_decision_id": decision.decision_id,
            "moves": [
                {"model_version": e.model_version, "to_stage": e.to_stage.value}
                for e in decision.events
            ],
        }

    def _champion(self) -> str | None:
        assert self.registry is not None  # noqa: S101 - checked by the caller
        row = self.registry.store.one_in_stage(self.registry.model_name, ModelStage.CHAMPION)
        return row.model_version if row else None
