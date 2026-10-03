from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from praxis.config import get_settings


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep tests independent of any developer .env or PRAXIS_* variables."""
    import os

    for key in list(os.environ):
        if key.startswith("PRAXIS_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(os.path.dirname(__file__))  # no .env discovery
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def valid_event() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event_id": "6f1c2c1e-6c1b-4a53-9d0e-2a1f6b7c8d90",
        "event_type": "customer.created",
        "source": "simulator",
        "occurred_at": "2026-10-03T12:00:00Z",
        "published_at": "2026-10-03T12:00:01Z",
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "correlation_id": "0b1a2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
        "causation_id": None,
        "entity_id": "cust_000001",
        "is_synthetic": True,
        "payload": {"tier": "standard"},
    }
