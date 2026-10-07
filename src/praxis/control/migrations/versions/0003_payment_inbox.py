"""Payment webhook inbox and provider object references (Phase 7).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07

``payment_webhook_inbox`` is the durable queue between the webhook endpoint (which only
inserts) and the asynchronous processor (which claims with ``FOR UPDATE SKIP LOCKED``).
Its primary key is the provider event id, so a duplicate delivery can never create a second
row. Only routing fields and a body checksum are stored: no payment details.

``payment_provider_refs`` maps a Praxis business key (customer id, plan lookup key) to the
provider object created for it, so ``ensure_*`` gateway calls stay idempotent beyond the
provider's 24-hour idempotency-key window. A mapping never changes once written.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE payment_webhook_inbox (
    provider            TEXT        NOT NULL CHECK (provider IN ('stripe', 'synthetic')),
    provider_event_id   TEXT        NOT NULL CHECK (length(provider_event_id) BETWEEN 1 AND 255),
    event_type          TEXT        NOT NULL,
    object_kind         TEXT        NOT NULL CHECK (object_kind IN ('invoice', 'subscription')),
    object_id           TEXT        NOT NULL CHECK (length(object_id) BETWEEN 1 AND 255),
    provider_created_at TIMESTAMPTZ NOT NULL,
    livemode            BOOLEAN     NOT NULL,
    api_version         TEXT,
    body_sha256         TEXT        NOT NULL CHECK (length(body_sha256) = 64),
    trace_id            TEXT        NOT NULL,
    correlation_id      TEXT        NOT NULL,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    deliveries          INTEGER     NOT NULL DEFAULT 1 CHECK (deliveries >= 1),
    status              TEXT        NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processed', 'ignored', 'failed')),
    attempts            INTEGER     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome             TEXT,
    last_error          TEXT,
    processed_at        TIMESTAMPTZ,
    PRIMARY KEY (provider, provider_event_id),
    CHECK ((status = 'pending') = (processed_at IS NULL))
);
CREATE INDEX payment_webhook_inbox_due ON payment_webhook_inbox (next_attempt_at)
    WHERE status = 'pending';
CREATE INDEX payment_webhook_inbox_object
    ON payment_webhook_inbox (provider, object_kind, object_id);

CREATE TABLE payment_provider_refs (
    provider    TEXT        NOT NULL CHECK (provider IN ('stripe', 'synthetic')),
    kind        TEXT        NOT NULL
        CHECK (kind IN ('customer', 'product', 'price', 'subscription')),
    praxis_key  TEXT        NOT NULL CHECK (length(praxis_key) BETWEEN 1 AND 255),
    provider_id TEXT        NOT NULL CHECK (length(provider_id) BETWEEN 1 AND 255),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, kind, praxis_key),
    UNIQUE (provider, kind, provider_id)
);
CREATE TRIGGER payment_provider_refs_append_only BEFORE UPDATE OR DELETE
    ON payment_provider_refs FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    op.execute("DROP TABLE payment_provider_refs, payment_webhook_inbox;")
