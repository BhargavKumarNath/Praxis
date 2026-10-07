"""Decision audit store and price book: a decision executes only from its persisted record.

``record`` is idempotent: the same decision (same id, same content) recorded twice is a
no-op, so a retried cycle never duplicates its audit trail. A different decision for a cycle
and product that already has one is a ``RecordConflict``, never an overwrite.

``execute`` reads the decision back from the store, verifies its checksum, and applies the
mode rules (shadow never; recommend after an approval; execute directly). On Postgres the
same rules are enforced again by a trigger (migration 0002), so no code path can execute a
price without an audit record. ``MemoryDecisionStore`` has identical semantics for offline
shadow evaluation and tests.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from sqlalchemy import Connection, Engine, exc, text

from praxis.control.db import transient_errors
from praxis.pricing.config import Mode
from praxis.pricing.decision import Decision, Status


class StoreError(RuntimeError):
    """The audit store refused an operation."""


class RecordConflict(StoreError):
    """A different decision already exists for this id or for this (cycle, product)."""


class NotExecutable(StoreError):
    """The decision may not be executed (mode, status, approval or integrity)."""


@dataclass(frozen=True)
class Execution:
    decision_id: str
    product: str
    from_price_micros: int | None
    to_price_micros: int
    executed_by: str
    executed_at: datetime


class DecisionStore(Protocol):
    def record(self, decision: Decision, *, trace_id: str | None = None) -> bool:
        """Persist; True if inserted, False if the identical record already existed."""
        ...

    def get_record(self, decision_id: str) -> dict[str, Any] | None: ...

    def approve(self, decision_id: str, approver: str) -> None: ...

    def execute(self, decision_id: str, executor: str) -> Execution: ...

    def executions(self, product: str | None = None) -> list[Execution]: ...


def _canonical(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(record: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(record).encode()).hexdigest()


def check_executable(
    record: dict[str, Any] | None, approved: bool, decision_id: str
) -> dict[str, Any]:
    """The mode / status / approval rules, shared by both stores. Returns the record."""
    if record is None:
        raise NotExecutable(f"no audit record for decision {decision_id}")
    if record["status"] != Status.CHANGE.value:
        raise NotExecutable(f"decision {decision_id} is {record['status']}, not a change")
    if record["mode"] == Mode.SHADOW.value:
        raise NotExecutable(f"shadow decision {decision_id} can never execute")
    if record["mode"] == Mode.RECOMMEND.value and not approved:
        raise NotExecutable(f"recommended decision {decision_id} has no approval")
    return record


def _check_approvable(record: dict[str, Any] | None, decision_id: str, approver: str) -> None:
    if not approver.strip():
        raise StoreError("approver must be named")
    if record is None:
        raise NotExecutable(f"no audit record for decision {decision_id}")
    if record["mode"] != Mode.RECOMMEND.value or record["status"] != Status.CHANGE.value:
        raise NotExecutable(f"decision {decision_id} is not an approvable recommendation")


# ------------------------------------------------------------------------- memory
class MemoryDecisionStore:
    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._by_cycle: dict[tuple[str, str], str] = {}
        self._approvals: dict[str, str] = {}
        self._executions: dict[str, Execution] = {}
        self._lock = threading.Lock()

    def record(self, decision: Decision, *, trace_id: str | None = None) -> bool:
        rec = decision.record()
        did = decision.decision_id
        with self._lock:
            existing = self._records.get(did)
            if existing is not None:
                if _sha(existing) != _sha(rec):
                    raise RecordConflict(f"decision {did} already recorded with other content")
                return False
            other = self._by_cycle.get((decision.cycle_id, decision.product))
            if other is not None:
                raise RecordConflict(
                    f"cycle {decision.cycle_id} already decided {decision.product} ({other})"
                )
            self._records[did] = rec
            self._by_cycle[(decision.cycle_id, decision.product)] = did
            return True

    def get_record(self, decision_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._records.get(decision_id)
            return None if rec is None else json.loads(json.dumps(rec))

    def approve(self, decision_id: str, approver: str) -> None:
        with self._lock:
            _check_approvable(self._records.get(decision_id), decision_id, approver)
            self._approvals.setdefault(decision_id, approver)

    def execute(self, decision_id: str, executor: str) -> Execution:
        if not executor.strip():
            raise StoreError("executor must be named")
        with self._lock:
            done = self._executions.get(decision_id)
            if done is not None:
                return done
            rec = check_executable(
                self._records.get(decision_id), decision_id in self._approvals, decision_id
            )
            ex = Execution(
                decision_id,
                rec["product"],
                rec["current_price_micros"],
                rec["chosen_price_micros"],
                executor,
                datetime.now(UTC),
            )
            self._executions[decision_id] = ex
            return ex

    def executions(self, product: str | None = None) -> list[Execution]:
        with self._lock:
            out = [e for e in self._executions.values() if product in (None, e.product)]
        return sorted(out, key=lambda e: (e.executed_at, e.decision_id))


# ----------------------------------------------------------------------- postgres
_INSERT = text(
    """
    INSERT INTO pricing_decisions (decision_id, cycle_id, product, as_of, mode, status,
        current_price_micros, chosen_price_micros, policy_version, reason_codes, record,
        record_sha256, trace_id)
    VALUES (:decision_id, :cycle_id, :product, :as_of, :mode, :status, :current, :chosen,
        :policy_version, :reason_codes, CAST(:record AS JSON), :sha, :trace_id)
    ON CONFLICT DO NOTHING
    """
)


class PostgresDecisionStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def record(self, decision: Decision, *, trace_id: str | None = None) -> bool:
        rec = decision.record()
        sha = _sha(rec)
        params = {
            "decision_id": decision.decision_id,
            "cycle_id": decision.cycle_id,
            "product": decision.product,
            "as_of": decision.as_of,
            "mode": decision.mode.value,
            "status": decision.status.value,
            "current": decision.current_price_micros,
            "chosen": decision.chosen_price_micros,
            "policy_version": decision.policy_version,
            "reason_codes": list(decision.reason_codes),
            "record": _canonical(rec),
            "sha": sha,
            "trace_id": trace_id,
        }
        with transient_errors(), self.engine.begin() as conn:
            if conn.execute(_INSERT, params).rowcount == 1:
                return True
            row = conn.execute(
                text(
                    "SELECT decision_id, record_sha256 FROM pricing_decisions "
                    "WHERE decision_id = :d OR (cycle_id = :c AND product = :p)"
                ),
                {"d": decision.decision_id, "c": decision.cycle_id, "p": decision.product},
            ).first()
        if row is not None and row[0] == decision.decision_id and row[1] == sha:
            return False
        raise RecordConflict(
            f"cycle {decision.cycle_id} / {decision.product} already has decision "
            f"{None if row is None else row[0]} with other content"
        )

    def get_record(self, decision_id: str) -> dict[str, Any] | None:
        with transient_errors(), self.engine.connect() as conn:
            return self._read(conn, decision_id)

    @staticmethod
    def _read(conn: Connection, decision_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            text(
                "SELECT record::text, record_sha256 FROM pricing_decisions WHERE decision_id = :d"
            ),
            {"d": decision_id},
        ).first()
        if row is None:
            return None
        if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            raise NotExecutable(f"audit record {decision_id} fails its checksum")
        record: dict[str, Any] = json.loads(row[0])
        return record

    def approve(self, decision_id: str, approver: str) -> None:
        with transient_errors(), self.engine.begin() as conn:
            _check_approvable(self._read(conn, decision_id), decision_id, approver)
            conn.execute(
                text(
                    "INSERT INTO pricing_approvals (decision_id, approved_by) "
                    "VALUES (:d, :a) ON CONFLICT DO NOTHING"
                ),
                {"d": decision_id, "a": approver},
            )

    def execute(self, decision_id: str, executor: str) -> Execution:
        if not executor.strip():
            raise StoreError("executor must be named")
        with transient_errors(), self.engine.begin() as conn:
            done = self._execution(conn, decision_id)
            if done is not None:
                return done
            approved = conn.execute(
                text("SELECT 1 FROM pricing_approvals WHERE decision_id = :d"), {"d": decision_id}
            ).first()
            rec = check_executable(self._read(conn, decision_id), approved is not None, decision_id)
            try:
                row = conn.execute(
                    text(
                        "INSERT INTO price_executions (decision_id, product, from_price_micros, "
                        "to_price_micros, executed_by) VALUES (:d, :p, :f, :t, :e) "
                        "RETURNING decision_id, product, from_price_micros, to_price_micros, "
                        "executed_by, executed_at"
                    ),
                    {
                        "d": decision_id,
                        "p": rec["product"],
                        "f": rec["current_price_micros"],
                        "t": rec["chosen_price_micros"],
                        "e": executor,
                    },
                ).one()
            except exc.IntegrityError as error:  # the trigger is the last line of defence
                raise NotExecutable(f"database refused execution of {decision_id}") from error
        return Execution(*row)

    @staticmethod
    def _execution(conn: Connection, decision_id: str) -> Execution | None:
        row = conn.execute(
            text(
                "SELECT decision_id, product, from_price_micros, to_price_micros, executed_by, "
                "executed_at FROM price_executions WHERE decision_id = :d"
            ),
            {"d": decision_id},
        ).first()
        return None if row is None else Execution(*row)

    def executions(self, product: str | None = None) -> list[Execution]:
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT decision_id, product, from_price_micros, to_price_micros, "
                    "executed_by, executed_at FROM price_executions "
                    "WHERE CAST(:p AS TEXT) IS NULL OR product = :p "
                    "ORDER BY executed_at, decision_id"
                ),
                {"p": product},
            ).all()
        return [Execution(*r) for r in rows]


def last_execution_date(store: DecisionStore, product: str) -> date | None:
    done = store.executions(product)
    return done[-1].executed_at.date() if done else None
