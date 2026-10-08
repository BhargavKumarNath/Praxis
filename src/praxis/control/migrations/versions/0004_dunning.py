"""Dunning cases, decisions and retry jobs (Phase 8, ADR 0015).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08

* ``dunning_cases``: one row per invoice whose collection failed; ``state`` is the access
  stage (``praxis.domain.dunning.DunningState``), guarded by the same trigger as the other
  machines (moves seeded below = the closure of the Python table; a test keeps them equal).
* ``dunning_decisions``: append-only record of every decision (policy kind and version,
  model version, fallback reason, features, plan, stage) before anything is scheduled.
* ``retry_jobs``: one scheduled charge attempt each, guarded by the retry-job machine.
  Two partial unique indexes make duplicate retry side effects unrepresentable:
  at most one LIVE job per invoice, and at most one job per (invoice, attempt number) that
  may have reached the provider (executing / succeeded / failed). ``enqueued_at`` is the
  transactional outbox flag: the row commits first, the Cloud Task is created after.
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

ALLOWED_MOVES = {
    "dunning": [
        ("closed", "closed"), ("grace", "closed"), ("grace", "grace"), ("grace", "recovered"),
        ("grace", "restricted"), ("grace", "suspended"), ("past_due", "closed"),
        ("past_due", "grace"), ("past_due", "past_due"), ("past_due", "recovered"),
        ("past_due", "restricted"), ("past_due", "suspended"), ("recovered", "recovered"),
        ("restricted", "closed"), ("restricted", "recovered"), ("restricted", "restricted"),
        ("restricted", "suspended"), ("suspended", "closed"), ("suspended", "recovered"),
        ("suspended", "suspended"),
    ],
    "retry_job": [
        ("cancelled", "cancelled"), ("executing", "executing"), ("executing", "failed"),
        ("executing", "succeeded"), ("expired", "expired"), ("failed", "failed"),
        ("scheduled", "cancelled"), ("scheduled", "executing"), ("scheduled", "expired"),
        ("scheduled", "failed"), ("scheduled", "scheduled"), ("scheduled", "succeeded"),
        ("scheduled", "superseded"), ("succeeded", "succeeded"), ("superseded", "superseded"),
    ],
}  # fmt: skip

SCHEMA = """
CREATE TABLE dunning_cases (
    invoice_id      TEXT        PRIMARY KEY,
    customer_id     TEXT        NOT NULL,
    provider        TEXT,       -- payment provider that owns the invoice; NULL = not chargeable
    state           TEXT        NOT NULL CHECK (state IN
        ('past_due', 'grace', 'restricted', 'suspended', 'recovered', 'closed')),
    amount_minor    BIGINT      NOT NULL CHECK (amount_minor > 0),
    currency        TEXT        NOT NULL,
    first_reason    TEXT,
    opened_at       TIMESTAMPTZ NOT NULL,   -- first failed attempt (elapsed time zero)
    attempts_seen   INTEGER     NOT NULL CHECK (attempts_seen >= 0),
    last_failure_at TIMESTAMPTZ,
    recovered_at    TIMESTAMPTZ,
    closed_reason   TEXT,
    version         INTEGER     NOT NULL DEFAULT 1,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((state = 'recovered') = (recovered_at IS NOT NULL))
);
CREATE INDEX dunning_cases_customer ON dunning_cases (customer_id);
CREATE TRIGGER dunning_cases_state_guard BEFORE UPDATE ON dunning_cases
    FOR EACH ROW EXECUTE FUNCTION guard_state_move('dunning');

CREATE TABLE dunning_decisions (
    decision_id         TEXT        PRIMARY KEY,
    invoice_id          TEXT        NOT NULL REFERENCES dunning_cases (invoice_id),
    attempts_made       INTEGER     NOT NULL CHECK (attempts_made >= 1),
    decided_at          TIMESTAMPTZ NOT NULL,
    action              TEXT        NOT NULL CHECK (action IN ('retry', 'stop')),
    retry_at            TIMESTAMPTZ,
    stage               TEXT        NOT NULL,
    policy_kind         TEXT        NOT NULL CHECK (policy_kind IN ('model', 'baseline')),
    policy_version      TEXT        NOT NULL,
    model_version       TEXT,
    fallback_reason     TEXT,
    expected_value_minor DOUBLE PRECISION,  -- model estimate, never an amount of money moved
    p_next_success      DOUBLE PRECISION,
    plan                JSONB       NOT NULL,
    features            JSONB,
    trace_id            TEXT        NOT NULL,
    event_id            UUID,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((action = 'retry') = (retry_at IS NOT NULL)),
    CHECK ((policy_kind = 'model') = (model_version IS NOT NULL))
);
CREATE INDEX dunning_decisions_invoice ON dunning_decisions (invoice_id);
CREATE TRIGGER dunning_decisions_append_only BEFORE UPDATE OR DELETE
    ON dunning_decisions FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE retry_jobs (
    job_id          TEXT        PRIMARY KEY,
    invoice_id      TEXT        NOT NULL REFERENCES dunning_cases (invoice_id),
    decision_id     TEXT        NOT NULL REFERENCES dunning_decisions (decision_id),
    attempt_number  INTEGER     NOT NULL CHECK (attempt_number BETWEEN 2 AND 6),
    run_at          TIMESTAMPTZ NOT NULL,
    task_name       TEXT        NOT NULL UNIQUE,
    idempotency_key TEXT        NOT NULL,
    state           TEXT        NOT NULL CHECK (state IN ('scheduled', 'executing',
        'succeeded', 'failed', 'cancelled', 'superseded', 'expired')),
    enqueued_at     TIMESTAMPTZ,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    dispatches      INTEGER     NOT NULL DEFAULT 0 CHECK (dispatches >= 0),
    outcome         TEXT,
    failure_reason  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX retry_jobs_one_live_per_invoice ON retry_jobs (invoice_id)
    WHERE state IN ('scheduled', 'executing');
CREATE UNIQUE INDEX retry_jobs_one_charge_per_attempt ON retry_jobs (invoice_id, attempt_number)
    WHERE state IN ('executing', 'succeeded', 'failed');
CREATE INDEX retry_jobs_unenqueued ON retry_jobs (created_at)
    WHERE state = 'scheduled' AND enqueued_at IS NULL;
CREATE TRIGGER retry_jobs_state_guard BEFORE UPDATE ON retry_jobs
    FOR EACH ROW EXECUTE FUNCTION guard_state_move('retry_job');
"""


def upgrade() -> None:
    op.execute(SCHEMA)
    rows = ", ".join(
        f"('{machine}', '{src}', '{dst}')"
        for machine, moves in ALLOWED_MOVES.items()
        for src, dst in moves
    )
    op.execute(f"INSERT INTO allowed_state_moves VALUES {rows}")  # noqa: S608 - static literals


def downgrade() -> None:
    op.execute(
        "DROP TABLE retry_jobs, dunning_decisions, dunning_cases; "
        "DELETE FROM allowed_state_moves WHERE machine IN ('dunning', 'retry_job');"
    )
