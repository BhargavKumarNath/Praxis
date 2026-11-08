"""The streaming event write path stores exactly what the batch loader stores.

ADR 0028: both paths land rows in ``raw.sim_events_landing`` and compact them into
``raw.sim_events`` with a date-scoped anti-join (no in-memory primary-key index). These tests
pin both to an independent reference: the first occurrence of each ``event_id`` across all
writes, in write order, is stored, once, whatever the batch sizes, the compaction cadence or
duplicated / changed content.
"""

from __future__ import annotations

import copy
import json
import tempfile
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


def _loader(w: Warehouse, rows: list[dict[str, Any]], batch_id: str) -> int:
    """The batch loader: one NDJSON file through ``load_sim_events``."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "e.ndjson"
        path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        return w.load_sim_events(path, {"batch_id": batch_id})


def _first_occurrences(plan: list[list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    first: dict[str, dict[str, Any]] = {}
    for rows in plan:
        for row in rows:
            first.setdefault(str(row["event_id"]), row)
    return first


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
@given(batches, st.sampled_from([0, 1, 7, 25, 1000]))
def test_streaming_insert_matches_the_loader(
    plan: list[list[tuple[int, bool]]], compact_every: int
) -> None:
    batches_ = [[_variant(POOL[k]) if changed else POOL[k] for k, changed in b] for b in plan]
    with Warehouse(None, compact_every=compact_every) as new, Warehouse(None) as ref:
        new.migrate()
        ref.migrate()
        made_visible = 0
        for i, rows in enumerate(batches_):
            made_visible += new.insert_events(rows, f"b{i}")
            assert _loader(ref, rows, f"b{i}") >= 0
        made_visible += new.flush()
        expected = _first_occurrences(batches_)
        assert made_visible == len(expected)
        assert _table(new) == _table(ref)
        assert [r[0] for r in _table(new)] == sorted(expected)
        assert [r[8] for r in _table(new)] == [expected[k]["trace_id"] for k in sorted(expected)]
        assert new.duplicate_event_ids() == 0
        assert new.count("raw.sim_events_landing") == 0


def test_deferred_compaction_is_durable_and_invisible_until_flush(tmp_path: Path) -> None:
    path = tmp_path / "w.duckdb"
    with Warehouse(path, compact_every=100) as w:
        w.migrate()
        assert w.insert_events(POOL[:10], "s") == 0
        assert w.count("raw.sim_events") == 0
        assert w.count("raw.sim_events_landing") == 10
    with Warehouse(path) as w:  # a restart: the landed rows survived
        w.migrate()
        assert w.flush() == 10
        assert w.insert_events(POOL[:12], "s") == 2
        assert w.count("raw.sim_events") == 12


def test_compaction_threshold_counts_landed_rows() -> None:
    with Warehouse(None, compact_every=5) as w:
        w.migrate()
        assert w.insert_events(POOL[:3], "s") == 0
        assert w.insert_events(POOL[2:6], "s") == 6  # 3 + 4 landed >= 5: compacted
        assert w.count("raw.sim_events_landing") == 0
        assert w.flush() == 0


def test_streaming_writers_compact_per_threshold_not_per_micro_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0028 amendment 1: compacting every micro-batch re-scans the growing table each time
    (measured: 1K chaos benchmark 1,800 -> 575 ev/s). A streaming writer compacts once per
    ``STREAM_COMPACT_ROWS`` landed rows, and ``flush()`` still stores every first occurrence."""
    assert wh.STREAM_COMPACT_ROWS >= 10_000
    calls: list[int] = []
    real = Warehouse._compact

    def counting(self: Warehouse) -> int:
        calls.append(1)
        return real(self)

    monkeypatch.setattr(Warehouse, "_compact", counting)
    with Warehouse(None, compact_every=wh.STREAM_COMPACT_ROWS) as w:
        w.migrate()
        for i in range(0, len(POOL), 3):  # 20 micro-batches of 3 rows, one duplicated
            assert w.insert_events([*POOL[i : i + 3], POOL[i]], "s") == 0
        assert calls == []
        assert w.flush() == len(POOL)
        assert calls == [1]
        assert w.count("raw.sim_events") == len(POOL)
        assert w.duplicate_event_ids() == 0


def test_negative_compaction_threshold_is_refused() -> None:
    with pytest.raises(ValueError, match="compact_every"):
        Warehouse(None, compact_every=-1)


def test_empty_and_fully_duplicate_batches_write_nothing() -> None:
    with Warehouse(None) as w:
        w.migrate()
        assert w.insert_events([], "s") == 0
        assert w.insert_events([POOL[0], _variant(POOL[0])], "s") == 1
        assert w.insert_events([_variant(POOL[0])], "s") == 0
        assert _table(w)[0][8] == POOL[0]["trace_id"]  # the first occurrence was kept
        assert w.count("raw.sim_events_landing") == 0


@pytest.mark.parametrize("size", [1, wh._MAX_BOUND_ROWS, wh._MAX_BOUND_ROWS + 1, 40])
def test_bound_and_file_landing_store_the_same_rows(size: int) -> None:
    with Warehouse(None) as a, Warehouse(None) as b:
        a.migrate()
        b.migrate()
        assert a.insert_events(POOL[:size], "s") == size
        assert _loader(b, POOL[:size], "s") == size
        assert _table(a) == _table(b)


def test_a_failed_compaction_rolls_back_and_keeps_the_landed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Warehouse(None, compact_every=1000) as w:
        w.migrate()
        w.insert_events(POOL[:5], "s")
        monkeypatch.setattr(wh, "_COMPACT", "INSERT INTO raw.no_such_table SELECT ?, ?")
        with pytest.raises(Exception, match="no_such_table"):
            w.flush()
        monkeypatch.undo()
        assert w.count("raw.sim_events_landing") == 5
        assert w.flush() == 5


def test_writers_of_one_file_share_one_lock(tmp_path: Path) -> None:
    path = tmp_path / "w.duckdb"
    with Warehouse(path) as a:
        a.migrate()
    with Warehouse(path) as a, Warehouse(path, read_only=False) as b:
        assert a._lock is b._lock
