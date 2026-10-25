"""Operational incidents and their audit events (Phase 11, ADR 0018).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-25

* ``ops_incidents``: one row per incident. A partial unique index allows at most one LIVE
  (open / acknowledged) incident per alert fingerprint, so duplicate or concurrent
  evaluations cannot open the same incident twice (the incident flow is idempotent).
* ``ops_incident_events``: append-only timeline (opened, acknowledged, resolved, ...).
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE ops_incidents (
    incident_id   TEXT PRIMARY KEY,
    fingerprint   TEXT NOT NULL,
    rule_id       TEXT NOT NULL,
    category      TEXT NOT NULL CHECK (category IN ('service', 'data', 'model', 'business')),
    severity      TEXT NOT NULL CHECK (severity IN ('page', 'ticket')),
    status        TEXT NOT NULL CHECK (status IN ('open', 'acknowledged', 'resolved')),
    summary       TEXT NOT NULL,
    runbook       TEXT NOT NULL,
    context       JSONB NOT NULL,
    synthetic     BOOLEAN NOT NULL,
    occurrences   INTEGER NOT NULL DEFAULT 1 CHECK (occurrences >= 1),
    clear_streak  INTEGER NOT NULL DEFAULT 0 CHECK (clear_streak >= 0),
    acknowledged_by TEXT,
    opened_at     TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    resolved_at   TIMESTAMPTZ,
    CHECK ((status = 'resolved') = (resolved_at IS NOT NULL))
);
CREATE UNIQUE INDEX ops_incidents_one_live ON ops_incidents (fingerprint)
    WHERE status <> 'resolved';
CREATE TABLE ops_incident_events (
    event_id    BIGSERIAL PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES ops_incidents (incident_id),
    at          TIMESTAMPTZ NOT NULL,
    kind        TEXT NOT NULL,
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ops_incident_events_incident ON ops_incident_events (incident_id, event_id);
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    op.execute("DROP TABLE ops_incident_events, ops_incidents;")
