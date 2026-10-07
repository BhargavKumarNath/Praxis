"""Pricing decision audit log, approvals and price executions (Phase 6).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06

The database itself guarantees that no price executes without an audit record:

* ``price_executions.decision_id`` references ``pricing_decisions`` (no record, no row);
* a trigger allows an execution only for a CHANGE decision, at exactly its chosen price,
  never in shadow mode, and in recommend mode only after a recorded approval;
* decisions, approvals and executions are append-only (UPDATE / DELETE raise).

Prices are BIGINT micro-GBP. One decision per (cycle, product).
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'check_violation';
END;
$$;

CREATE TABLE pricing_decisions (
    decision_id          TEXT        PRIMARY KEY,
    cycle_id             TEXT        NOT NULL,
    product              TEXT        NOT NULL,
    as_of                DATE        NOT NULL,
    mode                 TEXT        NOT NULL CHECK (mode IN ('shadow', 'recommend', 'execute')),
    status               TEXT        NOT NULL
        CHECK (status IN ('change', 'hold', 'frozen', 'infeasible', 'unavailable')),
    current_price_micros BIGINT      CHECK (current_price_micros > 0),
    chosen_price_micros  BIGINT      CHECK (chosen_price_micros > 0),
    policy_version       TEXT        NOT NULL,
    reason_codes         TEXT[]      NOT NULL CHECK (cardinality(reason_codes) >= 1),
    record               JSON        NOT NULL,  -- JSON keeps the exact canonical text
    record_sha256        TEXT        NOT NULL CHECK (length(record_sha256) = 64),
    trace_id             TEXT,
    recorded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (cycle_id, product),
    CHECK ((status = 'change') = (chosen_price_micros IS NOT NULL)),
    CHECK (chosen_price_micros IS NULL OR chosen_price_micros IS DISTINCT FROM current_price_micros)
);
CREATE INDEX pricing_decisions_product ON pricing_decisions (product, as_of);
CREATE TRIGGER pricing_decisions_append_only BEFORE UPDATE OR DELETE ON pricing_decisions
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE pricing_approvals (
    decision_id TEXT        PRIMARY KEY REFERENCES pricing_decisions (decision_id),
    approved_by TEXT        NOT NULL CHECK (length(approved_by) > 0),
    approved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER pricing_approvals_append_only BEFORE UPDATE OR DELETE ON pricing_approvals
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE FUNCTION guard_pricing_approval() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE d pricing_decisions%ROWTYPE;
BEGIN
    SELECT * INTO d FROM pricing_decisions WHERE decision_id = NEW.decision_id;
    IF d.mode <> 'recommend' OR d.status <> 'change' THEN
        RAISE EXCEPTION 'decision % (% / %) cannot be approved', d.decision_id, d.mode, d.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER pricing_approvals_guard BEFORE INSERT ON pricing_approvals
    FOR EACH ROW EXECUTE FUNCTION guard_pricing_approval();

-- The live price book: one row per executed decision.
CREATE TABLE price_executions (
    decision_id       TEXT        PRIMARY KEY REFERENCES pricing_decisions (decision_id),
    product           TEXT        NOT NULL,
    from_price_micros BIGINT      CHECK (from_price_micros > 0),
    to_price_micros   BIGINT      NOT NULL CHECK (to_price_micros > 0),
    executed_by       TEXT        NOT NULL CHECK (length(executed_by) > 0),
    executed_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX price_executions_product ON price_executions (product, executed_at);
CREATE TRIGGER price_executions_append_only BEFORE UPDATE OR DELETE ON price_executions
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE FUNCTION guard_price_execution() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE d pricing_decisions%ROWTYPE;
BEGIN
    SELECT * INTO d FROM pricing_decisions WHERE decision_id = NEW.decision_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no audit record for decision %', NEW.decision_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF d.status <> 'change' OR NEW.product <> d.product
            OR NEW.to_price_micros <> d.chosen_price_micros
            OR NEW.from_price_micros IS DISTINCT FROM d.current_price_micros THEN
        RAISE EXCEPTION 'execution does not match decision %', d.decision_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF d.mode = 'shadow' THEN
        RAISE EXCEPTION 'shadow decision % can never execute', d.decision_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF d.mode = 'recommend'
            AND NOT EXISTS (SELECT 1 FROM pricing_approvals a WHERE a.decision_id = d.decision_id)
            THEN
        RAISE EXCEPTION 'recommended decision % has no approval', d.decision_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER price_executions_guard BEFORE INSERT ON price_executions
    FOR EACH ROW EXECUTE FUNCTION guard_price_execution();
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    op.execute(
        "DROP TABLE price_executions, pricing_approvals, pricing_decisions; "
        "DROP FUNCTION guard_price_execution(); DROP FUNCTION guard_pricing_approval(); "
        "DROP FUNCTION forbid_mutation();"
    )
