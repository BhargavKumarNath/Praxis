"""Postgres test fixtures (pytest plugin, loaded from tests/conftest.py).

Every test gets its own database cloned from a migrated template, so tests are isolated
and fast. ``PRAXIS_TEST_DATABASE_URL`` (an admin URL to a throwaway server, e.g. the
``make pg-up`` container) is REQUIRED: control-plane tests are critical and must fail
loudly rather than skip when no database is available. ``make test`` / CI set it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from praxis.control.db import make_engine, migrate
from praxis.control.store import ControlPlaneStore

ENV = "PRAXIS_TEST_DATABASE_URL"
# Captured at import: the autouse settings fixture strips PRAXIS_* variables per test.
ADMIN_URL = os.environ.get(ENV)


def _admin() -> Engine:
    if not ADMIN_URL:
        pytest.fail(
            f"{ENV} is not set. Control-plane tests need Postgres: run `make pg-up` and "
            f"export {ENV}=postgresql+psycopg://praxis@127.0.0.1:55432/postgres "
            "(make test / make check do this for you)."
        )
    return create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")


def _url_for(database: str) -> str:
    assert ADMIN_URL is not None
    return make_url(ADMIN_URL).set(database=database).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def pg_template() -> Iterator[str]:
    admin = _admin()
    name = f"praxis_tpl_{os.getpid()}"
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    migrate(_url_for(name))
    yield name
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def pg_url_factory(pg_template: str) -> Iterator[Callable[[], str]]:
    """Create any number of fresh, migrated databases within one test."""
    admin = _admin()
    created: list[str] = []

    def make() -> str:
        name = f"praxis_t_{uuid.uuid4().hex[:12]}"
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{pg_template}"'))
        created.append(name)
        return _url_for(name)

    yield make
    with admin.connect() as conn:
        for name in created:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def pg_url(pg_url_factory: Callable[[], str]) -> str:
    return pg_url_factory()


@pytest.fixture
def empty_pg_url() -> Iterator[str]:
    """A database with no migrations applied (migration tests)."""
    admin = _admin()
    name = f"praxis_e_{uuid.uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    yield _url_for(name)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def pg_engine(pg_url: str) -> Iterator[Engine]:
    engine = make_engine(pg_url)
    yield engine
    engine.dispose()


@pytest.fixture
def store(pg_engine: Engine) -> ControlPlaneStore:
    return ControlPlaneStore(pg_engine)
