"""Cross-process metric snapshots (ADR 0020).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-25

* ``ops_metric_snapshots``: the latest in-process metric snapshot of each running process,
  one row per (component, instance), overwritten on every publish. Each publisher sets
  ``expires_at`` (a loop: a few publish intervals; a batch job: its cadence plus slack).
  Readers merge the unexpired rows into one fleet view and report expired rows as stale
  instead of using them. It is a small operational table, not a time-series store:
  history lives in the publishing process only.
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE ops_metric_snapshots (
    component    TEXT NOT NULL CHECK (component ~ '^[a-z][a-z0-9_.-]{0,62}$'),
    instance     TEXT NOT NULL CHECK (length(instance) BETWEEN 1 AND 200),
    published_at TIMESTAMPTZ NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    points       JSONB NOT NULL CHECK (jsonb_typeof(points) = 'array'),
    PRIMARY KEY (component, instance),
    CHECK (expires_at > published_at AND started_at <= published_at)
);
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    op.execute("DROP TABLE ops_metric_snapshots;")
