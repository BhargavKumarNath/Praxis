"""Control plane (Postgres): mutable operational state, idempotency store, dead letters.

Schema changes go through Alembic migrations in ``migrations/`` (ADR 0002, 0009).
"""
