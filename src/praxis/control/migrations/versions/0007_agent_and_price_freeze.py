"""AI operator audit, guarded action requests and price freezes (Phase 12, ADR 0019).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-25

* ``agent_audit_events``: append-only. Every agent tool call writes a ``received`` row
  before anything runs and an ``outcome`` row afterwards (refused, invalid and failed calls
  included), so audit completeness is checkable per ``call_id``.
* ``agent_action_requests``: a guarded (Tier 2) tool only creates a PENDING request. A named
  human completes it; the database refuses a decision by the requester itself. At most one
  pending request per (kind, target), so a repeated alarm joins the open request.
* ``pricing_freezes``: an approved freeze blocks price execution for one product or for all
  (``*``). The ``price_executions`` trigger refuses an execution while a freeze covers the
  product, so no code path can change a frozen price.
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE agent_audit_events (
    event_id    BIGSERIAL   PRIMARY KEY,
    call_id     TEXT        NOT NULL,
    session_id  TEXT        NOT NULL,
    actor       TEXT        NOT NULL CHECK (length(actor) > 0),
    tool        TEXT        NOT NULL,
    tier        SMALLINT    CHECK (tier BETWEEN 0 AND 2),
    phase       TEXT        NOT NULL CHECK (phase IN ('received', 'outcome', 'session')),
    status      TEXT        NOT NULL,
    args        JSONB       NOT NULL,
    detail      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    trace_id    TEXT,
    at          TIMESTAMPTZ NOT NULL
);
CREATE INDEX agent_audit_events_call ON agent_audit_events (call_id, event_id);
CREATE INDEX agent_audit_events_session ON agent_audit_events (session_id, event_id);
CREATE TRIGGER agent_audit_events_append_only BEFORE UPDATE OR DELETE ON agent_audit_events
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE agent_action_requests (
    request_id   TEXT        PRIMARY KEY,
    kind         TEXT        NOT NULL CHECK (kind IN ('freeze_pricing', 'rollback', 'promotion')),
    target       TEXT        NOT NULL,
    params       JSONB       NOT NULL,
    rationale    TEXT        NOT NULL,
    evidence     JSONB       NOT NULL,
    requested_by TEXT        NOT NULL CHECK (length(requested_by) > 0),
    session_id   TEXT        NOT NULL,
    status       TEXT        NOT NULL
        CHECK (status IN ('pending', 'executing', 'completed', 'failed', 'rejected')),
    decided_by   TEXT        CHECK (length(decided_by) > 0),
    decided_at   TIMESTAMPTZ,
    outcome      JSONB,
    created_at   TIMESTAMPTZ NOT NULL,
    CHECK (decided_by IS DISTINCT FROM requested_by),
    CHECK ((status = 'pending') = (decided_by IS NULL))
);
CREATE UNIQUE INDEX agent_action_requests_one_pending ON agent_action_requests (kind, target)
    WHERE status = 'pending';

CREATE TABLE pricing_freezes (
    freeze_id    TEXT        PRIMARY KEY,
    scope        TEXT        NOT NULL CHECK (length(scope) > 0),  -- product id or '*'
    reason       TEXT        NOT NULL,
    requested_by TEXT        NOT NULL CHECK (length(requested_by) > 0),
    approved_by  TEXT        NOT NULL CHECK (length(approved_by) > 0),
    started_at   TIMESTAMPTZ NOT NULL,
    lifted_at    TIMESTAMPTZ,
    lifted_by    TEXT,
    CHECK (approved_by IS DISTINCT FROM requested_by),
    CHECK ((lifted_at IS NULL) = (lifted_by IS NULL))
);
CREATE UNIQUE INDEX pricing_freezes_one_active ON pricing_freezes (scope) WHERE lifted_at IS NULL;

CREATE FUNCTION guard_price_freeze() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM pricing_freezes
               WHERE lifted_at IS NULL AND scope IN (NEW.product, '*')) THEN
        RAISE EXCEPTION 'pricing is frozen for %', NEW.product USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER price_executions_freeze_guard BEFORE INSERT ON price_executions
    FOR EACH ROW EXECUTE FUNCTION guard_price_freeze();
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER price_executions_freeze_guard ON price_executions; "
        "DROP FUNCTION guard_price_freeze(); "
        "DROP TABLE pricing_freezes, agent_action_requests, agent_audit_events;"
    )
