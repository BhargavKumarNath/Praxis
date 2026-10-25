"""Incident flow: firing alerts -> deduplicated incidents -> notification -> resolution.

* One live incident per alert fingerprint; a repeat evaluation only touches it.
* An incident auto-resolves after ``clear_after`` consecutive evaluations without its alert.
* Acknowledging and resolving by hand are explicit, timestamped, attributed actions.
* The incident is persisted BEFORE anyone is notified; a notifier failure is logged and
  recorded on the incident timeline, never lost and never blocks the flow.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import Engine, exc, text

from praxis.control.db import transient_errors
from praxis.ops.evaluate import Alert

logger = logging.getLogger(__name__)


class IncidentStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class IncidentError(ValueError):
    """An illegal incident action (unknown incident, already resolved, ...)."""


@dataclass(frozen=True)
class Incident:
    incident_id: str
    fingerprint: str
    rule_id: str
    category: str
    severity: str
    status: IncidentStatus
    summary: str
    runbook: str
    context: dict[str, Any]
    synthetic: bool
    opened_at: datetime
    updated_at: datetime
    occurrences: int = 1
    clear_streak: int = 0
    acknowledged_by: str | None = None
    resolved_at: datetime | None = None

    @property
    def live(self) -> bool:
        return self.status is not IncidentStatus.RESOLVED

    def as_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id, "rule_id": self.rule_id, "category": self.category,
            "severity": self.severity, "status": self.status.value, "summary": self.summary,
            "runbook": self.runbook, "synthetic": self.synthetic, "occurrences": self.occurrences,
            "opened_at": self.opened_at.isoformat(), "updated_at": self.updated_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "acknowledged_by": self.acknowledged_by, "context": self.context,
        }  # fmt: skip


def incident_id(fingerprint: str, opened_at: datetime) -> str:
    return (
        "inc_" + hashlib.sha256(f"{fingerprint}|{opened_at.isoformat()}".encode()).hexdigest()[:16]
    )


class IncidentStore(Protocol):
    def create(self, incident: Incident) -> bool:
        """Insert; False when a live incident with this fingerprint already exists."""

    def get(self, incident_id: str) -> Incident | None: ...
    def live(self) -> list[Incident]: ...
    def recent(self, limit: int = 50) -> list[Incident]: ...
    def save(self, incident: Incident, kind: str | None, detail: dict[str, Any]) -> None:
        """Persist new state; ``kind`` appends a timeline event (None = counters only)."""

    def events(self, incident_id: str) -> list[dict[str, Any]]: ...


class MemoryIncidentStore:
    def __init__(self) -> None:
        self._incidents: dict[str, Incident] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}

    def create(self, incident: Incident) -> bool:
        if any(i.fingerprint == incident.fingerprint and i.live for i in self._incidents.values()):
            return False
        self._incidents[incident.incident_id] = incident
        self._events[incident.incident_id] = [
            {"at": incident.opened_at, "kind": "opened", "detail": incident.context}
        ]
        return True

    def get(self, incident_id: str) -> Incident | None:
        return self._incidents.get(incident_id)

    def live(self) -> list[Incident]:
        return [i for i in self._incidents.values() if i.live]

    def recent(self, limit: int = 50) -> list[Incident]:
        ordered = sorted(self._incidents.values(), key=lambda i: i.opened_at, reverse=True)
        return ordered[:limit]

    def save(self, incident: Incident, kind: str | None, detail: dict[str, Any]) -> None:
        self._incidents[incident.incident_id] = incident
        if kind:
            self._events[incident.incident_id].append(
                {"at": incident.updated_at, "kind": kind, "detail": detail}
            )

    def events(self, incident_id: str) -> list[dict[str, Any]]:
        return list(self._events.get(incident_id, []))


_COLS = (
    "incident_id, fingerprint, rule_id, category, severity, status, summary, runbook, context, "
    "synthetic, opened_at, updated_at, occurrences, clear_streak, acknowledged_by, resolved_at"
)


class PostgresIncidentStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create(self, incident: Incident) -> bool:
        params = _row_params(incident)
        try:
            with transient_errors(), self.engine.begin() as conn:
                conn.execute(
                    text(
                        f"INSERT INTO ops_incidents ({_COLS}) VALUES (:incident_id, :fingerprint, "  # noqa: S608
                        ":rule_id, :category, :severity, :status, :summary, :runbook, "
                        "CAST(:context AS JSONB), :synthetic, :opened_at, :updated_at, "
                        ":occurrences, :clear_streak, :acknowledged_by, :resolved_at)"
                    ),
                    params,
                )
                self._event(conn, incident, "opened", incident.context)
        except exc.IntegrityError:
            return False  # the one-live-per-fingerprint index
        return True

    @staticmethod
    def _event(conn: Any, incident: Incident, kind: str, detail: dict[str, Any]) -> None:
        conn.execute(
            text(
                "INSERT INTO ops_incident_events (incident_id, at, kind, detail) "
                "VALUES (:i, :at, :k, CAST(:d AS JSONB))"
            ),
            {"i": incident.incident_id, "at": incident.updated_at, "k": kind,
             "d": json.dumps(detail, default=str)},
        )  # fmt: skip

    def get(self, incident_id: str) -> Incident | None:
        with transient_errors(), self.engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT {_COLS} FROM ops_incidents WHERE incident_id = :i"),  # noqa: S608
                {"i": incident_id},
            ).first()
        return _from_row(row) if row else None

    def live(self) -> list[Incident]:
        return self._query("WHERE status <> 'resolved' ORDER BY opened_at", {})

    def recent(self, limit: int = 50) -> list[Incident]:
        return self._query("ORDER BY opened_at DESC LIMIT :n", {"n": limit})

    def _query(self, tail: str, params: dict[str, Any]) -> list[Incident]:
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(text(f"SELECT {_COLS} FROM ops_incidents {tail}"), params).all()  # noqa: S608
        return [_from_row(r) for r in rows]

    def save(self, incident: Incident, kind: str | None, detail: dict[str, Any]) -> None:
        with transient_errors(), self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE ops_incidents SET status = :status, context = CAST(:context AS JSONB), "
                    "occurrences = :occurrences, clear_streak = :clear_streak, "
                    "acknowledged_by = :acknowledged_by, updated_at = :updated_at, "
                    "resolved_at = :resolved_at WHERE incident_id = :incident_id"
                ),
                _row_params(incident),
            )
            if kind:
                self._event(conn, incident, kind, detail)

    def events(self, incident_id: str) -> list[dict[str, Any]]:
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT at, kind, detail FROM ops_incident_events "
                    "WHERE incident_id = :i ORDER BY event_id"
                ),
                {"i": incident_id},
            ).all()
        return [{"at": r[0], "kind": r[1], "detail": r[2]} for r in rows]


def _row_params(i: Incident) -> dict[str, Any]:
    return {
        "incident_id": i.incident_id, "fingerprint": i.fingerprint, "rule_id": i.rule_id,
        "category": i.category, "severity": i.severity, "status": i.status.value,
        "summary": i.summary, "runbook": i.runbook,
        "context": json.dumps(i.context, default=str), "synthetic": i.synthetic,
        "opened_at": i.opened_at, "updated_at": i.updated_at, "occurrences": i.occurrences,
        "clear_streak": i.clear_streak, "acknowledged_by": i.acknowledged_by,
        "resolved_at": i.resolved_at,
    }  # fmt: skip


def _from_row(r: Any) -> Incident:
    return Incident(
        r[0], r[1], r[2], r[3], r[4], IncidentStatus(r[5]), r[6], r[7], r[8], r[9],
        r[10], r[11], r[12], r[13], r[14], r[15],
    )  # fmt: skip


class Notifier(Protocol):
    def notify(self, event: str, incident: Incident) -> None: ...


class LogNotifier:
    """Structured-log notification: the local stand-in for a pager / chat integration."""

    def notify(self, event: str, incident: Incident) -> None:
        level = logging.ERROR if incident.severity == "page" else logging.WARNING
        logger.log(
            level,
            "incident.%s",
            event,
            extra={
                "incident_id": incident.incident_id, "rule_id": incident.rule_id,
                "category": incident.category, "severity": incident.severity,
                "runbook": incident.runbook, "synthetic": incident.synthetic,
            },
        )  # fmt: skip


@dataclass
class RecordingNotifier:
    sent: list[tuple[str, Incident]] = field(default_factory=list)

    def notify(self, event: str, incident: Incident) -> None:
        self.sent.append((event, incident))


@dataclass(frozen=True)
class IncidentReport:
    opened: tuple[Incident, ...] = ()
    touched: tuple[Incident, ...] = ()
    resolved: tuple[Incident, ...] = ()


class IncidentManager:
    def __init__(self, store: IncidentStore, notifier: Notifier, *, clear_after: int = 2) -> None:
        if clear_after < 1:
            raise ValueError("clear_after must be >= 1")
        self.store = store
        self.notifier = notifier
        self.clear_after = clear_after

    def process(self, alerts: list[Alert], now: datetime | None = None) -> IncidentReport:
        now = now or datetime.now(UTC)
        live = {i.fingerprint: i for i in self.store.live()}
        opened: list[Incident] = []
        touched: list[Incident] = []
        for alert in alerts:
            current = live.get(alert.fingerprint)
            if current is None:
                created = self._open(alert, now)
                if created is not None:
                    opened.append(created)
                    continue
                current = next(i for i in self.store.live() if i.fingerprint == alert.fingerprint)
            touched.append(self._touch(current, alert, now))
        firing = {a.fingerprint for a in alerts}
        resolved = [self._clear_step(i, now) for fp, i in live.items() if fp not in firing]
        return IncidentReport(
            tuple(opened), tuple(touched), tuple(i for i in resolved if i is not None)
        )

    def _open(self, alert: Alert, now: datetime) -> Incident | None:
        incident = Incident(
            incident_id(alert.fingerprint, now), alert.fingerprint, alert.rule_id,
            alert.category, alert.severity, IncidentStatus.OPEN, alert.summary,
            alert.runbook_path, alert.context(), alert.synthetic, now, now,
        )  # fmt: skip
        if not self.store.create(incident):
            return None
        self._notify("opened", incident)
        return incident

    def _touch(self, current: Incident, alert: Alert, now: datetime) -> Incident:
        updated = replace(
            current, occurrences=current.occurrences + 1, clear_streak=0, updated_at=now,
            context=alert.context(),
        )  # fmt: skip
        self.store.save(updated, None, {})
        return updated

    def _clear_step(self, current: Incident, now: datetime) -> Incident | None:
        streak = current.clear_streak + 1
        if streak < self.clear_after:
            self.store.save(replace(current, clear_streak=streak, updated_at=now), None, {})
            return None
        return self._resolve(current, now, "auto_resolved", {"clear_evaluations": streak})

    def _resolve(
        self, current: Incident, now: datetime, kind: str, detail: dict[str, Any]
    ) -> Incident:
        resolved = replace(
            current, status=IncidentStatus.RESOLVED, resolved_at=now, updated_at=now,
            clear_streak=current.clear_streak + 1,
        )  # fmt: skip
        self.store.save(resolved, kind, detail)
        self._notify("resolved", resolved)
        return resolved

    def acknowledge(self, incident_id_: str, by: str, now: datetime | None = None) -> Incident:
        current = self._require_live(incident_id_)
        now = now or datetime.now(UTC)
        acked = replace(
            current, status=IncidentStatus.ACKNOWLEDGED, acknowledged_by=by, updated_at=now
        )
        self.store.save(acked, "acknowledged", {"by": by})
        return acked

    def resolve(self, incident_id_: str, by: str, now: datetime | None = None) -> Incident:
        current = self._require_live(incident_id_)
        return self._resolve(current, now or datetime.now(UTC), "resolved", {"by": by})

    def _require_live(self, incident_id_: str) -> Incident:
        current = self.store.get(incident_id_)
        if current is None or not current.live:
            raise IncidentError(f"no live incident {incident_id_}")
        return current

    def _notify(self, event: str, incident: Incident) -> None:
        try:
            self.notifier.notify(event, incident)
        except Exception as error:
            # The incident is already durable; record that nobody was told and move on.
            logger.exception(
                "incident notification failed", extra={"incident": incident.incident_id}
            )
            self.store.save(incident, "notify_failed", {"error_type": type(error).__name__})
