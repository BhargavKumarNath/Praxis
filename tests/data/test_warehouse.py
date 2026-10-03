from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from praxis.data.models import SignalRecord, SourceId, make_record_key
from praxis.data.warehouse import MIGRATIONS_DIR, MigrationError, Warehouse
from tests.data.helpers import NOW, cfg

OBS = datetime(2026, 1, 5, 3, tzinfo=UTC)


def _rec(value: float = 1.0, retrieved_at: datetime = NOW, metric: str = "m") -> SignalRecord:
    return SignalRecord(
        record_key=make_record_key(SourceId.FRED, "s", "us", metric, OBS),
        source=SourceId.FRED,
        series_id="s",
        entity_id="us",
        metric=metric,
        unit="u",
        observed_at=OBS,
        value=value,
        batch_id="b" * 64,
        retrieved_at=retrieved_at,
    )


@pytest.fixture
def wh() -> Warehouse:
    w = Warehouse()
    w.migrate()
    return w


def test_migrations_apply_once_and_are_idempotent(wh: Warehouse) -> None:
    assert wh.migrate() == []
    versions = [
        r[0] for r in wh.con.execute("SELECT version FROM ops.schema_migrations").fetchall()
    ]
    assert versions == sorted(p.stem for p in MIGRATIONS_DIR.glob("*.sql"))


def test_edited_migration_is_refused(wh: Warehouse) -> None:
    wh.con.execute("UPDATE ops.schema_migrations SET checksum = 'tampered'")
    with pytest.raises(MigrationError, match="modified"):
        wh.migrate()


def test_upsert_same_records_twice_creates_no_duplicates(wh: Warehouse) -> None:
    assert wh.upsert_signals([_rec(1.0), _rec(2.0, metric="n")]) == 2
    assert wh.upsert_signals([_rec(1.0), _rec(2.0, metric="n")]) == 0
    assert wh.count("raw.external_signals") == 2


def test_duplicates_inside_one_batch_collapse(wh: Warehouse) -> None:
    assert wh.upsert_signals([_rec(1.0), _rec(1.0)]) == 1


def test_newer_revision_overwrites_but_older_does_not(wh: Warehouse) -> None:
    wh.upsert_signals([_rec(1.0)])
    wh.upsert_signals([_rec(9.0, retrieved_at=NOW + timedelta(days=1))])
    assert wh.con.execute("SELECT value FROM raw.external_signals").fetchone() == (9.0,)
    wh.upsert_signals([_rec(5.0, retrieved_at=NOW - timedelta(days=1))])
    assert wh.con.execute("SELECT value FROM raw.external_signals").fetchone() == (9.0,)


def test_observed_date_is_utc_date_and_timestamps_are_utc(wh: Warehouse) -> None:
    wh.upsert_signals([_rec()])
    row = wh.con.execute(
        "SELECT CAST(observed_date AS VARCHAR), CAST(observed_at AS VARCHAR) "
        "FROM raw.external_signals"
    ).fetchone()
    assert row == ("2026-01-05", "2026-01-05 03:00:00+00")


def test_latest_observation_ignores_future_slots(wh: Warehouse) -> None:
    wh.upsert_signals([_rec()])
    assert wh.latest_observation(SourceId.FRED, NOW) == OBS
    assert wh.latest_observation(SourceId.FRED, OBS - timedelta(hours=1)) is None
    assert wh.latest_observation(SourceId.EIA, NOW) is None


def test_region_locations_load_is_replace_not_append(wh: Warehouse) -> None:
    wh.load_region_locations(cfg().locations)
    wh.load_region_locations(cfg().locations)
    assert wh.count("raw.region_locations") == len(cfg().locations)


def _event(i: int, day: int = 5) -> dict[str, object]:
    return {
        "causation_id": None,
        "correlation_id": "62086809-6f6e-43cb-876a-d7679675f768",
        "entity_id": "cust_00000001",
        "event_id": f"00000000-0000-4000-8000-{i:012d}",
        "event_type": "customer.created",
        "is_synthetic": True,
        "occurred_at": f"2026-01-{day:02d}T23:30:00Z",
        "payload": {"tier": "starter"},
        "published_at": f"2026-01-{day:02d}T23:30:00Z",
        "schema_version": 1,
        "source": "simulator",
        "trace_id": "a" * 32,
    }


def test_sim_event_load_is_idempotent_and_tolerates_duplicate_lines(
    wh: Warehouse, tmp_path: Path
) -> None:
    path = tmp_path / "events.ndjson"
    lines = [_event(1), _event(2, day=6), _event(1)]  # exact duplicate delivery
    path.write_text("\n".join(json.dumps(e) for e in lines) + "\n")
    manifest = {"batch_id": "batch-1"}
    assert wh.load_sim_events(path, manifest) == 2
    assert wh.load_sim_events(path, manifest) == 0
    assert wh.count("raw.sim_events") == 2
    assert wh.count("raw.sim_batches") == 1
    dates = wh.con.execute(
        "SELECT CAST(event_date AS VARCHAR) FROM raw.sim_events ORDER BY 1"
    ).fetchall()
    assert dates == [("2026-01-05",), ("2026-01-06",)]  # 23:30Z stays on its UTC date


def test_partitioned_parquet_export_layout(wh: Warehouse, tmp_path: Path) -> None:
    src = tmp_path / "e.ndjson"
    src.write_text("\n".join(json.dumps(_event(i, day=5 + i % 3)) for i in range(6)) + "\n")
    wh.load_sim_events(src, {"batch_id": "x"})
    out = tmp_path / "parquet"
    assert wh.export_partitioned_parquet("raw.sim_events", out, "event_date") == 3
    assert sorted(p.name for p in out.glob("event_date=*")) == [
        "event_date=2026-01-05",
        "event_date=2026-01-06",
        "event_date=2026-01-07",
    ]


@pytest.mark.parametrize(
    ("table", "col"), [("x; DROP TABLE y", "event_date"), ("raw.sim_events", "a b")]
)
def test_export_rejects_non_identifiers(
    wh: Warehouse, tmp_path: Path, table: str, col: str
) -> None:
    with pytest.raises(ValueError, match="identifiers"):
        wh.export_partitioned_parquet(table, tmp_path / "o", col)


def test_export_rejects_quote_in_path(wh: Warehouse, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="quote"):
        wh.export_partitioned_parquet("raw.sim_events", tmp_path / "o'x", "event_date")


def test_record_batch_upserts_on_batch_id(wh: Warehouse) -> None:
    from praxis.data.models import BatchProvenance, QualityStatus

    prov = BatchProvenance(
        batch_id="b1",
        source=SourceId.FRED,
        series_id="s",
        endpoint="e",
        retrieved_at=NOW,
        checksum_sha256="c",
        http_status=200,
        quality_status=QualityStatus.OK,
    )
    wh.record_batch(prov)
    wh.record_batch(prov.model_copy(update={"quality_status": QualityStatus.PARTIAL}))
    rows = wh.con.execute("SELECT quality_status FROM raw.ingest_batches").fetchall()
    assert rows == [("partial",)]
