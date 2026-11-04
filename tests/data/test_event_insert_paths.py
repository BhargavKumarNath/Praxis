"""The streaming event write path stores exactly what the batch loader's ON CONFLICT does.

``Warehouse.insert_events`` (ADR 0025 amendment 1) replaces ``ON CONFLICT`` with a primary-key
lookup and a plain INSERT of new rows, bound as parameters for small batches and as NDJSON for
larger ones. These tests pin it to the reference statement ``_INSERT_EVENTS``.
"""

from __future__ import annotations

import copy
import json
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from praxis.data import warehouse as wh
from praxis.data.warehouse import Warehouse
from praxis.events.envelope import EventEnvelope
from praxis.simulator.config import load_config
from praxis.simulator.engine import Engine
from praxis.simulator.population import generate_population


def _pool() -> list[dict[str, Any]]:
    cfg = load_config().with_overrides(n_customers=12, days=20)
    events = Engine(cfg, 7, generate_population(cfg, 7)).run()
    return [EventEnvelope.model_validate(e).model_dump(mode="json") for e in events][:60]


POOL = _pool()
COLUMNS = (
    "event_id, event_type, source, schema_version, entity_id, occurred_at, published_at, "
    "event_date, trace_id, correlation_id, causation_id, is_synthetic, CAST(payload AS VARCHAR), "
    "batch_id"
)


def _reference(w: Warehouse, rows: list[dict[str, Any]], batch_id: str) -> int:
    """The batch loader's statement: NDJSON + ``ON CONFLICT (event_id) DO NOTHING``."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "e.ndjson"
        path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        out = w._con.execute(wh._INSERT_EVENTS, [batch_id, datetime.now(UTC), str(path)])
        row = out.fetchone()
    return int(row[0]) if row else 0


def _table(w: Warehouse) -> list[tuple[Any, ...]]:
    return w._con.execute(f"SELECT {COLUMNS} FROM raw.sim_events ORDER BY event_id").fetchall()  # noqa: S608


def _variant(row: dict[str, Any]) -> dict[str, Any]:
    """Same id, different content: only the first occurrence may be stored."""
    other = copy.deepcopy(row)
    other["trace_id"] = "changed-trace"
    other["payload"] = {"changed": True}
    return other


batches = st.lists(
    st.lists(st.tuples(st.integers(0, len(POOL) - 1), st.booleans()), max_size=30),
    min_size=1,
    max_size=6,
)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(batches)
def test_streaming_insert_matches_the_on_conflict_loader(
    plan: list[list[tuple[int, bool]]],
) -> None:
    with Warehouse(None) as new, Warehouse(None) as ref:
        new.migrate()
        ref.migrate()
        for i, batch in enumerate(plan):
            rows = [_variant(POOL[k]) if changed else POOL[k] for k, changed in batch]
            assert new.insert_events(rows, f"b{i}") == _reference(ref, rows, f"b{i}")
        assert _table(new) == _table(ref)


@pytest.mark.parametrize("size", [1, wh._MAX_BOUND_ROWS, wh._MAX_BOUND_ROWS + 1, 40])
def test_a_concurrent_writer_falls_back_to_on_conflict(
    monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    rows = POOL[:size]
    with Warehouse(None) as w:
        w.migrate()
        original: Callable[..., int] = Warehouse._insert_new_events
        calls: list[bool] = []

        def racing(
            self: Warehouse, new: list[dict[str, Any]], batch_id: str, *, conflict: bool
        ) -> int:
            calls.append(conflict)
            if not conflict:  # another writer lands one row between lookup and insert
                _reference(self, new[-1:], "other-writer")
            return original(self, new, batch_id, conflict=conflict)

        monkeypatch.setattr(Warehouse, "_insert_new_events", racing)
        assert w.insert_events(rows, "s") == size - 1
        assert calls == [False, True]
        assert w.count("raw.sim_events") == size
        assert w.insert_events(rows, "s") == 0  # all present: lookup only, no write
        assert calls == [False, True]


def test_empty_and_fully_duplicate_batches_write_nothing() -> None:
    with Warehouse(None) as w:
        w.migrate()
        assert w.insert_events([], "s") == 0
        assert w.insert_events([POOL[0], _variant(POOL[0])], "s") == 1
        assert w.insert_events([_variant(POOL[0])], "s") == 0
        assert _table(w)[0][8] == POOL[0]["trace_id"]  # the first occurrence was kept
