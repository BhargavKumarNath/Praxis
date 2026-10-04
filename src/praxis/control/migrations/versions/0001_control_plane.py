"""Control plane: aggregates, event log, idempotency store, ledger, dead letters.

Revision ID: 0001
Revises:
Create Date: 2026-10-04

Money is BIGINT minor units. Times are TIMESTAMPTZ. State columns are guarded twice: a
CHECK constraint on the value set, and a trigger that only allows moves listed in
``allowed_state_moves`` (the reachability closure of the Python transition tables; a test
keeps the two identical). An UPDATE that would move a state backwards or sideways fails.
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

ALLOWED_MOVES = {
    "customer": [
        ("active", "active"), ("active", "churned"), ("churned", "churned"),
        ("converted", "active"), ("converted", "churned"), ("converted", "converted"),
        ("lost", "lost"), ("prospect", "active"), ("prospect", "churned"),
        ("prospect", "converted"), ("prospect", "lost"), ("prospect", "prospect"),
    ],
    "subscription": [("active", "active"), ("active", "cancelled"), ("cancelled", "cancelled")],
    "invoice": [
        ("attempting", "attempting"), ("attempting", "open"), ("attempting", "paid"),
        ("attempting", "uncollectible"), ("open", "attempting"), ("open", "open"),
        ("open", "paid"), ("open", "uncollectible"), ("paid", "paid"),
        ("uncollectible", "uncollectible"),
    ],
}  # fmt: skip

SCHEMA = """
CREATE TABLE allowed_state_moves (
    machine    TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state   TEXT NOT NULL,
    PRIMARY KEY (machine, from_state, to_state)
);

CREATE FUNCTION guard_state_move() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state IS DISTINCT FROM NEW.state AND NOT EXISTS (
        SELECT 1 FROM allowed_state_moves
        WHERE machine = TG_ARGV[0] AND from_state = OLD.state AND to_state = NEW.state
    ) THEN
        RAISE EXCEPTION 'forbidden % state move % -> %', TG_ARGV[0], OLD.state, NEW.state
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

-- Idempotency store: one row per (consumer, event). Inserted in the same transaction as
-- the consumer's side effects, so "side effect committed" and "event marked processed"
-- are atomic. A redelivery finds the row and becomes a no-op.
CREATE TABLE processed_events (
    consumer         TEXT        NOT NULL,
    event_id         UUID        NOT NULL,
    event_type       TEXT        NOT NULL,
    aggregate_kind   TEXT,
    aggregate_key    TEXT,
    trace_id         TEXT        NOT NULL,
    correlation_id   TEXT        NOT NULL,
    delivery_attempt INTEGER     NOT NULL,
    processed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, event_id)
);
CREATE INDEX processed_events_correlation ON processed_events (correlation_id);

-- Accepted lifecycle events per aggregate (bounded: lifecycle events only, ADR 0002).
CREATE TABLE entity_events (
    event_id       UUID        PRIMARY KEY,
    aggregate_kind TEXT        NOT NULL CHECK (aggregate_kind IN ('customer', 'invoice')),
    aggregate_key  TEXT        NOT NULL,
    event_type     TEXT        NOT NULL,
    entity_id      TEXT        NOT NULL,
    occurred_at    TIMESTAMPTZ NOT NULL,
    payload        JSONB       NOT NULL,
    trace_id       TEXT        NOT NULL,
    correlation_id TEXT        NOT NULL,
    causation_id   UUID,
    received_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX entity_events_aggregate ON entity_events (aggregate_kind, aggregate_key);

CREATE TABLE customers (
    customer_id     TEXT        PRIMARY KEY,
    state           TEXT        NOT NULL
        CHECK (state IN ('prospect', 'converted', 'active', 'churned', 'lost')),
    region_id       TEXT        NOT NULL,
    industry        TEXT        NOT NULL,
    tier            TEXT        NOT NULL,
    is_existing     BOOLEAN     NOT NULL,
    churn_reason    TEXT,
    applied_count   INTEGER     NOT NULL CHECK (applied_count >= 1),
    pending_count   INTEGER     NOT NULL CHECK (pending_count >= 0),
    last_applied_at TIMESTAMPTZ NOT NULL,
    version         INTEGER     NOT NULL DEFAULT 1,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER customers_state_guard BEFORE UPDATE ON customers
    FOR EACH ROW EXECUTE FUNCTION guard_state_move('customer');

CREATE TABLE subscriptions (
    customer_id         TEXT        PRIMARY KEY REFERENCES customers (customer_id),
    state               TEXT        NOT NULL CHECK (state IN ('active', 'cancelled')),
    products            TEXT[]      NOT NULL,
    base_fee_minor      BIGINT      NOT NULL CHECK (base_fee_minor >= 0),
    billing_period_days INTEGER     NOT NULL CHECK (billing_period_days > 0),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER subscriptions_state_guard BEFORE UPDATE ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION guard_state_move('subscription');

-- No FK to customers: the two aggregates' events may arrive in either order.
CREATE TABLE invoices (
    invoice_id          TEXT        PRIMARY KEY,
    customer_id         TEXT        NOT NULL,
    state               TEXT        NOT NULL
        CHECK (state IN ('open', 'attempting', 'paid', 'uncollectible')),
    amount_minor        BIGINT      NOT NULL CHECK (amount_minor > 0),
    currency            TEXT        NOT NULL,
    period_start        DATE        NOT NULL,
    period_end          DATE        NOT NULL,
    attempts            INTEGER     NOT NULL CHECK (attempts >= 0),
    amount_paid_minor   BIGINT      NOT NULL CHECK (amount_paid_minor >= 0),
    last_failure_reason TEXT,
    applied_count       INTEGER     NOT NULL CHECK (applied_count >= 1),
    pending_count       INTEGER     NOT NULL CHECK (pending_count >= 0),
    last_applied_at     TIMESTAMPTZ NOT NULL,
    version             INTEGER     NOT NULL DEFAULT 1,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (amount_paid_minor IN (0, amount_minor)),
    CHECK ((state = 'paid') = (amount_paid_minor = amount_minor))
);
CREATE INDEX invoices_customer ON invoices (customer_id);
CREATE TRIGGER invoices_state_guard BEFORE UPDATE ON invoices
    FOR EACH ROW EXECUTE FUNCTION guard_state_move('invoice');

-- Money actually collected. One row per invoice at most: a duplicate success can never
-- collect twice, whatever the delivery pattern.
CREATE TABLE payment_ledger (
    invoice_id   TEXT        PRIMARY KEY,
    customer_id  TEXT        NOT NULL,
    amount_minor BIGINT      NOT NULL CHECK (amount_minor > 0),
    currency     TEXT        NOT NULL,
    event_id     UUID        NOT NULL UNIQUE,
    paid_at      TIMESTAMPTZ NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Audit trail of applied transitions (append-only, keyed by the causing event).
CREATE TABLE state_transitions (
    machine       TEXT        NOT NULL,
    aggregate_key TEXT        NOT NULL,
    event_id      UUID        NOT NULL,
    from_state    TEXT,
    to_state      TEXT        NOT NULL,
    occurred_at   TIMESTAMPTZ NOT NULL,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (machine, aggregate_key, event_id)
);

-- Dead letters, recorded by the DLQ inspector. Keyed so redelivered DLQ copies dedupe.
CREATE TABLE dead_letters (
    dead_letter_key     TEXT        PRIMARY KEY,
    source_subscription TEXT        NOT NULL,
    reason              TEXT        NOT NULL,
    detail              TEXT,
    event_id            TEXT,
    event_type          TEXT,
    trace_id            TEXT,
    correlation_id      TEXT,
    delivery_attempt    INTEGER,
    data_sha256         TEXT        NOT NULL,
    data_size           INTEGER     NOT NULL,
    data                BYTEA,  -- original bytes when <= 64 KiB, for inspection / redrive
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    redriven_at         TIMESTAMPTZ  -- set when an operator republished it after a fix
);
CREATE INDEX dead_letters_reason ON dead_letters (reason);
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
        "DROP TABLE dead_letters, state_transitions, payment_ledger, invoices, subscriptions, "
        "customers, entity_events, processed_events, allowed_state_moves; "
        "DROP FUNCTION guard_state_move();"
    )
