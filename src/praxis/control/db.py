"""Engine factory and migrations for the control plane.

Connections have bounded timeouts (connect and statement) so a stuck database turns into
a ``TransientError`` and a redelivery, never a hung consumer. Sessions run in UTC.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, exc

from praxis.streaming.transport import TransientError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def make_engine(
    url: str,
    *,
    connect_timeout_s: int = 5,
    statement_timeout_ms: int = 10_000,
    pool_size: int = 5,
) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=0,
        pool_timeout=connect_timeout_s,
        connect_args={
            "connect_timeout": connect_timeout_s,
            "options": f"-c timezone=UTC -c statement_timeout={statement_timeout_ms}",
        },
    )


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def migrate(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def is_transient(error: BaseException) -> bool:
    """Connection loss, timeouts, deadlocks and serialization failures may succeed later."""
    if isinstance(error, exc.OperationalError | exc.InterfaceError | exc.TimeoutError):
        return True
    return isinstance(error, exc.DBAPIError) and bool(error.connection_invalidated)


@contextmanager
def transient_errors() -> Iterator[None]:
    """Re-raise transient database failures as ``TransientError`` (NACK, redeliver)."""
    try:
        yield
    except exc.SQLAlchemyError as error:
        if is_transient(error):
            raise TransientError(f"database unavailable: {type(error).__name__}") from error
        raise
