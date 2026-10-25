"""Price freezes: an approved freeze blocks price execution for one product or for all.

A freeze is protective (it can only stop prices moving), but it is still a guarded action:
it is created only with a named approver who is not the requester (the AI operator requests,
a human approves; ADR 0019), and lifting it is an attributed human action.

Enforcement is at execution: ``MemoryDecisionStore`` consults its freeze store, and on
Postgres the ``price_executions`` trigger (migration 0007) refuses the insert, so neither the
pricing cycle nor the CLI can execute a frozen product. Decisions are still made and recorded
while frozen; shadow evaluation is unaffected.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import Engine, exc, text

from praxis.control.db import transient_errors

ALL_PRODUCTS = "*"


class FreezeError(RuntimeError):
    """A freeze action was refused (unnamed approver, self-approval, already frozen, ...)."""


@dataclass(frozen=True)
class Freeze:
    freeze_id: str
    scope: str
    reason: str
    requested_by: str
    approved_by: str
    started_at: datetime
    lifted_at: datetime | None = None
    lifted_by: str | None = None

    @property
    def active(self) -> bool:
        return self.lifted_at is None

    def covers(self, product: str) -> bool:
        return self.active and self.scope in (product, ALL_PRODUCTS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "freeze_id": self.freeze_id, "scope": self.scope, "reason": self.reason,
            "requested_by": self.requested_by, "approved_by": self.approved_by,
            "started_at": self.started_at.isoformat(), "active": self.active,
            "lifted_at": self.lifted_at.isoformat() if self.lifted_at else None,
            "lifted_by": self.lifted_by,
        }  # fmt: skip


def freeze_id(scope: str, started_at: datetime) -> str:
    return "frz_" + hashlib.sha256(f"{scope}|{started_at.isoformat()}".encode()).hexdigest()[:16]


def _check_new(scope: str, requested_by: str, approved_by: str) -> None:
    if not scope.strip():
        raise FreezeError("freeze scope must be a product or '*'")
    if not approved_by.strip() or not requested_by.strip():
        raise FreezeError("a freeze needs a named requester and a named approver")
    if approved_by == requested_by:
        raise FreezeError("a freeze cannot be approved by its requester")


class FreezeStore(Protocol):
    def activate(
        self, scope: str, reason: str, *, requested_by: str, approved_by: str,
        now: datetime | None = None,
    ) -> Freeze: ...  # fmt: skip

    def lift(self, freeze_id_: str, *, by: str, now: datetime | None = None) -> Freeze: ...
    def active(self) -> list[Freeze]: ...
    def is_frozen(self, product: str) -> bool: ...


class MemoryFreezeStore:
    def __init__(self) -> None:
        self._freezes: dict[str, Freeze] = {}
        self._lock = threading.Lock()

    def activate(
        self, scope: str, reason: str, *, requested_by: str, approved_by: str,
        now: datetime | None = None,
    ) -> Freeze:  # fmt: skip
        _check_new(scope, requested_by, approved_by)
        now = now or datetime.now(UTC)
        with self._lock:
            if any(f.active and f.scope == scope for f in self._freezes.values()):
                raise FreezeError(f"{scope} is already frozen")
            frozen = Freeze(freeze_id(scope, now), scope, reason, requested_by, approved_by, now)
            self._freezes[frozen.freeze_id] = frozen
            return frozen

    def lift(self, freeze_id_: str, *, by: str, now: datetime | None = None) -> Freeze:
        if not by.strip():
            raise FreezeError("lifting a freeze needs a named operator")
        with self._lock:
            current = self._freezes.get(freeze_id_)
            if current is None or not current.active:
                raise FreezeError(f"no active freeze {freeze_id_}")
            lifted = replace(current, lifted_at=now or datetime.now(UTC), lifted_by=by)
            self._freezes[freeze_id_] = lifted
            return lifted

    def active(self) -> list[Freeze]:
        with self._lock:
            return sorted(
                (f for f in self._freezes.values() if f.active), key=lambda f: f.started_at
            )

    def is_frozen(self, product: str) -> bool:
        return any(f.covers(product) for f in self.active())


_COLS = "freeze_id, scope, reason, requested_by, approved_by, started_at, lifted_at, lifted_by"


class PostgresFreezeStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def activate(
        self, scope: str, reason: str, *, requested_by: str, approved_by: str,
        now: datetime | None = None,
    ) -> Freeze:  # fmt: skip
        _check_new(scope, requested_by, approved_by)
        now = now or datetime.now(UTC)
        frozen = Freeze(freeze_id(scope, now), scope, reason, requested_by, approved_by, now)
        try:
            with transient_errors(), self.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO pricing_freezes (freeze_id, scope, reason, requested_by, "
                        "approved_by, started_at) VALUES (:i, :s, :r, :q, :a, :t)"
                    ),
                    {"i": frozen.freeze_id, "s": scope, "r": reason, "q": requested_by,
                     "a": approved_by, "t": now},
                )  # fmt: skip
        except exc.IntegrityError as error:
            raise FreezeError(f"{scope} is already frozen") from error
        return frozen

    def lift(self, freeze_id_: str, *, by: str, now: datetime | None = None) -> Freeze:
        if not by.strip():
            raise FreezeError("lifting a freeze needs a named operator")
        with transient_errors(), self.engine.begin() as conn:
            row = conn.execute(
                text(
                    f"UPDATE pricing_freezes SET lifted_at = :t, lifted_by = :b "  # noqa: S608
                    f"WHERE freeze_id = :i AND lifted_at IS NULL RETURNING {_COLS}"
                ),
                {"t": now or datetime.now(UTC), "b": by, "i": freeze_id_},
            ).first()
        if row is None:
            raise FreezeError(f"no active freeze {freeze_id_}")
        return Freeze(*row)

    def active(self) -> list[Freeze]:
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT {_COLS} FROM pricing_freezes "  # noqa: S608 - constant columns
                    "WHERE lifted_at IS NULL ORDER BY started_at"
                )
            ).all()
        return [Freeze(*r) for r in rows]

    def is_frozen(self, product: str) -> bool:
        return any(f.covers(product) for f in self.active())
