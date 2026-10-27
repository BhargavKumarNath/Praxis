"""Console API fixtures: the REAL console app over a fresh migrated Postgres database and a
small DuckDB warehouse. Approver tokens are generated at runtime (no secret-looking literals
in the repository)."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from praxis.config import Settings
from praxis.console.app import create_console_app
from praxis.console.auth import Approvers, parse_approvers
from praxis.console.context import ConsoleContext
from praxis.ops.system import build_reader


@dataclass(frozen=True)
class Console:
    client: TestClient
    engine: Engine
    token: str  # belongs to the approver "alice"
    other_token: str  # belongs to the approver "bob"

    def auth(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}"}


def approvers_with(alice: str, bob: str) -> Approvers:
    return parse_approvers(f"alice={alice},bob={bob}")


def make_console(
    engine: Engine | None, warehouse: Path | None, approvers: Approvers | None = None
) -> TestClient:
    reader = build_reader(engine, warehouse)
    ctx = ConsoleContext(engine, warehouse, reader)
    app = create_console_app(
        Settings(), context=ctx, approvers=approvers if approvers is not None else Approvers()
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def console(pg_engine: Engine, tmp_path: Path) -> Iterator[Console]:
    alice, bob = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    client = make_console(pg_engine, tmp_path / "absent.duckdb", approvers_with(alice, bob))
    with client:
        yield Console(client, pg_engine, alice, bob)
