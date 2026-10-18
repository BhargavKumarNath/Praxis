"""Postgres registry: model versions, lifecycle audit, reports, retraining requests (0005).

The control plane is the authoritative registry (ADR 0017). Every stage change is checked
twice: in Python against ``praxis.domain.model_lifecycle`` and in Postgres by the guard
trigger; the partial unique indexes make a second champion / shadow / canary impossible.
A multi-version decision (promotion, rollback) is one transaction under one decision id.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, Engine, exc, text

from praxis.control.db import transient_errors
from praxis.domain.model_lifecycle import (
    MODEL_TRANSITIONS,
    RETRAINING_TRANSITIONS,
    ModelStage,
    RetrainingStatus,
)


class RegistryError(RuntimeError):
    """A registry operation is not allowed (unknown version, conflicting content, ...)."""


@dataclass(frozen=True)
class VersionRecord:
    """Everything registered for one artifact (lineage is immutable once stored)."""

    model_name: str
    model_version: str
    artifact_uri: str
    artifact_checksum: str
    data_version: str
    feature_version: str
    code_revision: str
    params: dict[str, Any]
    metrics: dict[str, Any]
    lineage: dict[str, Any]
    trained_at: datetime
    is_synthetic: bool
    reference: dict[str, Any] | None = None


@dataclass(frozen=True)
class VersionRow:
    record: VersionRecord
    stage: ModelStage
    stage_changed_at: datetime
    tracking_run_id: str | None

    @property
    def model_version(self) -> str:
        return self.record.model_version


@dataclass(frozen=True)
class Move:
    model_version: str
    trigger: str


@dataclass(frozen=True)
class StageEvent:
    event_id: int
    decision_id: str
    model_version: str
    from_stage: ModelStage | None
    to_stage: ModelStage
    trigger: str
    actor: str
    reason: str
    evidence: dict[str, Any]
    trace_id: str
    occurred_at: datetime


@dataclass(frozen=True)
class MonitoringReport:
    report_id: str
    model_name: str
    model_version: str
    kind: str
    as_of: date
    window_start: date
    window_end: date
    fired: bool
    signals: dict[str, Any]


@dataclass(frozen=True)
class RetrainingRequest:
    request_id: str
    model_name: str
    status: RetrainingStatus
    triggers: tuple[str, ...]
    evidence: dict[str, Any]
    as_of: date
    source_report_id: str | None
    champion_version: str | None
    candidate_version: str | None
    failure_reason: str | None


@dataclass(frozen=True)
class Audit:
    actor: str
    reason: str
    trace_id: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _require[T](value: T | None) -> T:
    if value is None:  # pragma: no cover - the row was written in this transaction
        raise RegistryError("row vanished inside its own transaction")
    return value


def content_id(prefix: str, *parts: object) -> str:
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}-{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


def _js(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


_VERSION_COLS = (
    "model_name, model_version, artifact_uri, artifact_checksum, data_version, "
    "feature_version, code_revision, params, metrics, lineage, trained_at, is_synthetic, "
    "reference, stage, stage_changed_at, tracking_run_id"
)


def _version(row: Sequence[Any]) -> VersionRow:
    record = VersionRecord(*row[:13])
    return VersionRow(record, ModelStage(row[13]), row[14], row[15])


def _event(row: Sequence[Any]) -> StageEvent:
    return StageEvent(
        event_id=int(row[0]),
        decision_id=row[1],
        model_version=row[2],
        from_stage=ModelStage(row[3]) if row[3] is not None else None,
        to_stage=ModelStage(row[4]),
        trigger=row[5],
        actor=row[6],
        reason=row[7],
        evidence=row[8],
        trace_id=row[9],
        occurred_at=row[10],
    )


_REQUEST_COLS = (
    "request_id, model_name, status, triggers, evidence, as_of, source_report_id, "
    "champion_version, candidate_version, failure_reason"
)


def _request(row: Sequence[Any]) -> RetrainingRequest:
    return RetrainingRequest(
        request_id=row[0],
        model_name=row[1],
        status=RetrainingStatus(row[2]),
        triggers=tuple(row[3]),
        evidence=row[4],
        as_of=row[5],
        source_report_id=row[6],
        champion_version=row[7],
        candidate_version=row[8],
        failure_reason=row[9],
    )


class RegistryStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # ----------------------------------------------------------------- versions
    def register(self, record: VersionRecord, audit: Audit) -> tuple[VersionRow, bool]:
        """Insert a CANDIDATE (idempotent per content). Returns (row, created)."""
        with transient_errors(), self.engine.begin() as conn:
            existing = self._get(conn, record.model_version, lock=True)
            if existing is not None:
                if existing.record.artifact_checksum != record.artifact_checksum:
                    raise RegistryError(
                        f"{record.model_version} is registered with a different checksum"
                    )
                return existing, False
            conn.execute(
                text(
                    "INSERT INTO model_versions (model_name, model_version, artifact_uri, "
                    "artifact_checksum, data_version, feature_version, code_revision, params, "
                    "metrics, lineage, trained_at, is_synthetic, reference, stage) VALUES "
                    "(:model_name, :model_version, :artifact_uri, :artifact_checksum, "
                    ":data_version, :feature_version, :code_revision, CAST(:params AS JSONB), "
                    "CAST(:metrics AS JSONB), CAST(:lineage AS JSONB), :trained_at, "
                    ":is_synthetic, CAST(:reference AS JSONB), 'candidate')"
                ),
                {
                    "model_name": record.model_name,
                    "model_version": record.model_version,
                    "artifact_uri": record.artifact_uri,
                    "artifact_checksum": record.artifact_checksum,
                    "data_version": record.data_version,
                    "feature_version": record.feature_version,
                    "code_revision": record.code_revision,
                    "params": _js(record.params),
                    "metrics": _js(record.metrics),
                    "lineage": _js(record.lineage),
                    "trained_at": record.trained_at,
                    "is_synthetic": record.is_synthetic,
                    "reference": None if record.reference is None else _js(record.reference),
                },
            )
            decision = content_id("reg", record.model_version)
            self._insert_event(
                conn,
                decision,
                record.model_name,
                record.model_version,
                None,
                ModelStage.CANDIDATE,
                "registered",
                audit,
            )
            return _require(self._get(conn, record.model_version, lock=False)), True

    def get(self, model_version: str) -> VersionRow | None:
        with transient_errors(), self.engine.connect() as conn:
            return self._get(conn, model_version, lock=False)

    def require(self, model_version: str) -> VersionRow:
        row = self.get(model_version)
        if row is None:
            raise RegistryError(f"unknown model version {model_version}")
        return row

    def in_stage(self, model_name: str, stage: ModelStage) -> list[VersionRow]:
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT {_VERSION_COLS} FROM model_versions "  # noqa: S608 - constant
                    "WHERE model_name = :n AND stage = :s ORDER BY stage_changed_at, model_version"
                ),
                {"n": model_name, "s": stage.value},
            ).all()
        return [_version(r) for r in rows]

    def one_in_stage(self, model_name: str, stage: ModelStage) -> VersionRow | None:
        rows = self.in_stage(model_name, stage)
        return rows[0] if rows else None

    def set_tracking_run(self, model_version: str, run_id: str) -> None:
        with transient_errors(), self.engine.begin() as conn:
            conn.execute(
                text("UPDATE model_versions SET tracking_run_id = :r WHERE model_version = :v"),
                {"r": run_id, "v": model_version},
            )

    def move(
        self, model_name: str, moves: Sequence[Move], decision_id: str, audit: Audit
    ) -> list[StageEvent]:
        """Apply several lifecycle moves atomically (in order) under one decision id."""
        with transient_errors(), self.engine.begin() as conn:
            events = []
            for mv in moves:
                row = self._get(conn, mv.model_version, lock=True)
                if row is None or row.record.model_name != model_name:
                    raise RegistryError(f"unknown {model_name} version {mv.model_version}")
                target = MODEL_TRANSITIONS.apply(row.stage, mv.trigger)
                try:
                    conn.execute(
                        text(
                            "UPDATE model_versions SET stage = :s, stage_changed_at = now(), "
                            "version = version + 1 WHERE model_version = :v"
                        ),
                        {"s": target.value, "v": mv.model_version},
                    )
                except exc.IntegrityError as error:
                    raise RegistryError(
                        f"{mv.model_version} -> {target.value} conflicts: {error.orig}"
                    ) from error
                events.append(
                    self._insert_event(
                        conn,
                        decision_id,
                        model_name,
                        mv.model_version,
                        row.stage,
                        target,
                        mv.trigger,
                        audit,
                    )
                )
            return events

    def events(
        self,
        *,
        model_name: str | None = None,
        model_version: str | None = None,
        decision_id: str | None = None,
    ) -> list[StageEvent]:
        clauses, params = [], {}
        for col, value in (
            ("model_name", model_name),
            ("model_version", model_version),
            ("decision_id", decision_id),
        ):
            if value is not None:
                clauses.append(f"{col} = :{col}")
                params[col] = value
        where = " AND ".join(clauses) or "TRUE"
        cols = "event_id, decision_id, model_version, from_stage, to_stage, trigger, actor, reason"
        sql = f"SELECT {cols}, evidence, trace_id, occurred_at FROM model_stage_events WHERE {where} ORDER BY event_id"  # noqa: S608, E501 - fixed column names
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).all()
        return [_event(r) for r in rows]

    def replaced_by(self, champion_version: str) -> str | None:
        """The champion that ``champion_version``'s last promotion retired (rollback target)."""
        events = self.events(model_version=champion_version)
        promotions = [e for e in events if e.to_stage == ModelStage.CHAMPION]
        if not promotions:
            return None
        decision = promotions[-1].decision_id
        retired = [
            e.model_version
            for e in self.events(decision_id=decision)
            if e.trigger == "replaced" and e.model_version != champion_version
        ]
        return retired[0] if retired else None

    # ---------------------------------------------------------------- reports
    def add_validation_report(
        self, model_version: str, failed_checks: Sequence[str], report: Mapping[str, Any]
    ) -> str:
        report_id = content_id("val", model_version, report)
        with transient_errors(), self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO model_validation_reports (report_id, model_version, passed, "
                    "failed_checks, report) VALUES (:id, :v, :p, :f, CAST(:r AS JSONB)) "
                    "ON CONFLICT DO NOTHING"
                ),
                {
                    "id": report_id,
                    "v": model_version,
                    "p": not failed_checks,
                    "f": list(failed_checks),
                    "r": _js(report),
                },
            )
        return report_id

    def validation_reports(self, model_version: str) -> list[dict[str, Any]]:
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT report FROM model_validation_reports WHERE model_version = :v "
                    "ORDER BY created_at, report_id"
                ),
                {"v": model_version},
            ).all()
        return [r[0] for r in rows]

    def add_monitoring_report(self, report: MonitoringReport) -> bool:
        """Idempotent: False when this exact report was already stored."""
        with transient_errors(), self.engine.begin() as conn:
            row = conn.execute(
                text(
                    "INSERT INTO model_monitoring_reports (report_id, model_name, model_version, "
                    "kind, as_of, window_start, window_end, fired, signals) VALUES (:id, :n, :v, "
                    ":k, :a, :ws, :we, :f, CAST(:s AS JSONB)) ON CONFLICT DO NOTHING "
                    "RETURNING report_id"
                ),
                {
                    "id": report.report_id,
                    "n": report.model_name,
                    "v": report.model_version,
                    "k": report.kind,
                    "a": report.as_of,
                    "ws": report.window_start,
                    "we": report.window_end,
                    "f": report.fired,
                    "s": _js(report.signals),
                },
            ).first()
        return row is not None

    def monitoring_reports(self, model_version: str, kind: str) -> list[MonitoringReport]:
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT report_id, model_name, model_version, kind, as_of, window_start, "
                    "window_end, fired, signals FROM model_monitoring_reports "
                    "WHERE model_version = :v AND kind = :k ORDER BY as_of, report_id"
                ),
                {"v": model_version, "k": kind},
            ).all()
        return [MonitoringReport(*r) for r in rows]

    # ------------------------------------------------------------- retraining
    def request_retraining(
        self,
        model_name: str,
        triggers: Sequence[str],
        evidence: Mapping[str, Any],
        as_of: date,
        source_report_id: str | None,
        champion_version: str | None,
    ) -> tuple[RetrainingRequest, bool]:
        """Open a request, or join the live one (at most one per model). (request, created).

        Idempotent: the id is a content hash of (model, data date, triggers), so re-running
        a day returns the request that day already made, whatever its status now."""
        request_id = content_id("rt", model_name, as_of, sorted(triggers))
        with transient_errors(), self.engine.begin() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"retraining:{model_name}"},
            )
            existing = self._get_request(conn, request_id)
            if existing is not None:
                return existing, False
            live = self._live_request(conn, model_name)
            if live is not None:
                return live, False
            conn.execute(
                text(
                    "INSERT INTO retraining_requests (request_id, model_name, status, triggers, "
                    "evidence, as_of, source_report_id, champion_version) VALUES (:id, :n, "
                    "'open', :t, CAST(:e AS JSONB), :a, :src, :champ)"
                ),
                {
                    "id": request_id,
                    "n": model_name,
                    "t": sorted(triggers),
                    "e": _js(evidence),
                    "a": as_of,
                    "src": source_report_id,
                    "champ": champion_version,
                },
            )
            return _require(self._get_request(conn, request_id)), True

    def live_request(self, model_name: str) -> RetrainingRequest | None:
        with transient_errors(), self.engine.connect() as conn:
            return self._live_request(conn, model_name)

    def get_request(self, request_id: str) -> RetrainingRequest | None:
        with transient_errors(), self.engine.connect() as conn:
            return self._get_request(conn, request_id)

    def requests(self, model_name: str) -> list[RetrainingRequest]:
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT {_REQUEST_COLS} FROM retraining_requests "  # noqa: S608 - constant
                    "WHERE model_name = :n ORDER BY created_at, request_id"
                ),
                {"n": model_name},
            ).all()
        return [_request(r) for r in rows]

    def advance_request(
        self,
        request_id: str,
        trigger: str,
        *,
        candidate_version: str | None = None,
        failure_reason: str | None = None,
    ) -> RetrainingRequest:
        with transient_errors(), self.engine.begin() as conn:
            current = self._get_request(conn, request_id, lock=True)
            if current is None:
                raise RegistryError(f"unknown retraining request {request_id}")
            status = RETRAINING_TRANSITIONS.apply(current.status, trigger)
            conn.execute(
                text(
                    "UPDATE retraining_requests SET status = :s, candidate_version = :c, "
                    "failure_reason = :f, updated_at = now() WHERE request_id = :id"
                ),
                {
                    "s": status.value,
                    "c": candidate_version,
                    "f": failure_reason,
                    "id": request_id,
                },
            )
            return _require(self._get_request(conn, request_id))

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _get(conn: Connection, model_version: str, *, lock: bool) -> VersionRow | None:
        row = conn.execute(
            text(
                f"SELECT {_VERSION_COLS} FROM model_versions "  # noqa: S608 - constant
                f"WHERE model_version = :v{' FOR UPDATE' if lock else ''}"
            ),
            {"v": model_version},
        ).first()
        return _version(row) if row is not None else None

    @staticmethod
    def _get_request(
        conn: Connection, request_id: str, *, lock: bool = False
    ) -> RetrainingRequest | None:
        row = conn.execute(
            text(
                f"SELECT {_REQUEST_COLS} FROM retraining_requests "  # noqa: S608 - constant
                f"WHERE request_id = :id{' FOR UPDATE' if lock else ''}"
            ),
            {"id": request_id},
        ).first()
        return _request(row) if row is not None else None

    @staticmethod
    def _live_request(conn: Connection, model_name: str) -> RetrainingRequest | None:
        row = conn.execute(
            text(
                f"SELECT {_REQUEST_COLS} FROM retraining_requests "  # noqa: S608 - constant
                "WHERE model_name = :n AND status IN ('open', 'training')"
            ),
            {"n": model_name},
        ).first()
        return _request(row) if row is not None else None

    @staticmethod
    def _insert_event(  # noqa: PLR0913 - one audit row
        conn: Connection,
        decision_id: str,
        model_name: str,
        model_version: str,
        from_stage: ModelStage | None,
        to_stage: ModelStage,
        trigger: str,
        audit: Audit,
    ) -> StageEvent:
        row = conn.execute(
            text(
                "INSERT INTO model_stage_events (decision_id, model_name, model_version, "
                "from_stage, to_stage, trigger, actor, reason, evidence, trace_id) VALUES "
                "(:d, :n, :v, :f, :t, :tr, :a, :r, CAST(:e AS JSONB), :trace) RETURNING "
                "event_id, decision_id, model_version, from_stage, to_stage, trigger, actor, "
                "reason, evidence, trace_id, occurred_at"
            ),
            {
                "d": decision_id,
                "n": model_name,
                "v": model_version,
                "f": from_stage.value if from_stage else None,
                "t": to_stage.value,
                "tr": trigger,
                "a": audit.actor,
                "r": audit.reason,
                "e": _js(audit.evidence),
                "trace": audit.trace_id,
            },
        ).one()
        return _event(row)
