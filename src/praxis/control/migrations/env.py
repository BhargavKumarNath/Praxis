"""Alembic environment. The URL always comes from the caller (never from a checked-in ini)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

url = context.config.get_main_option("sqlalchemy.url")
if not url:
    raise RuntimeError("sqlalchemy.url must be set by the caller (praxis.control.db.migrate)")

if context.is_offline_mode():
    context.configure(url=url, literal_binds=True, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
