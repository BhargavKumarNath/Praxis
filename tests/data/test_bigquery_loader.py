"""BigQuery loader, offline. The google client is faked at the network boundary only; schema
derivation, export files and job configuration are the real code against a real DuckDB."""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from google.cloud import bigquery

from praxis.data.bigquery import (
    DUCKDB_TO_BIGQUERY,
    PARTITIONING,
    RAW_TABLES,
    BigQueryLoader,
    LoadMismatchError,
    export_ndjson_gz,
    raw_columns,
)
from praxis.data.config import load_sources_config
from praxis.data.ingest import IngestService
from praxis.data.models import SourceId
from praxis.data.raw_store import LocalRawStore
from praxis.data.warehouse import Warehouse
from praxis.simulator.config import load_config
from praxis.simulator.runner import run_simulation
from tests.data.helpers import WINDOW, fixture_handler, make_fetcher, registry


@dataclass
class FakeJob:
    output_rows: int | None
    total_bytes_processed: int | None = None

    def result(self, timeout: float | None = None) -> FakeJob:
        return self


@dataclass
class FakeClient:
    """Records calls; ``drop_rows`` simulates BigQuery loading fewer rows than sent."""

    drop_rows: int = 0
    datasets: list[bigquery.Dataset] = field(default_factory=list)
    loads: list[dict[str, Any]] = field(default_factory=list)

    expiration_ms: int | None = None

    def get_dataset(self, dataset_id: str, timeout: float) -> Any:
        return type("DS", (), {"default_partition_expiration_ms": self.expiration_ms})()

    def create_dataset(self, dataset: bigquery.Dataset, exists_ok: bool, timeout: float) -> None:
        assert exists_ok
        self.datasets.append(dataset)

    def load_table_from_file(
        self, fh: Any, destination: str, job_config: Any, location: str
    ) -> FakeJob:
        lines = gzip.decompress(fh.read()).decode().splitlines()
        self.loads.append(
            {
                "destination": destination,
                "config": job_config,
                "rows": [json.loads(x) for x in lines],
            }
        )
        return FakeJob(output_rows=len(lines) - self.drop_rows)


@pytest.fixture(scope="module")
def wh(tmp_path_factory: pytest.TempPathFactory) -> Warehouse:
    root = tmp_path_factory.mktemp("bqload")
    sim = root / "sim"
    run_simulation(load_config().with_overrides(n_customers=15, days=3), 5, out_dir=sim)
    w = Warehouse()
    w.migrate()
    w.load_region_locations(load_sources_config().locations)
    w.load_sim_events(sim / "events.ndjson", json.loads((sim / "manifest.json").read_text()))
    service = IngestService(
        registry(), make_fetcher(fixture_handler()), LocalRawStore(root / "raw"), w
    )
    for sid in SourceId:
        service.ingest(sid, WINDOW)
    return w


def _loader(client: FakeClient) -> BigQueryLoader:
    return BigQueryLoader(
        client,  # type: ignore[arg-type]
        project="praxis-dev-test",
        prefix="praxis_dev_",
        location="europe-west2",
    )


def test_every_raw_column_type_has_a_bigquery_mapping(wh: Warehouse) -> None:
    for table in RAW_TABLES:
        columns = raw_columns(wh, table)
        assert columns and all(c.bq_type in DUCKDB_TO_BIGQUERY.values() for c in columns)
    types = {c.name: c.bq_type for c in raw_columns(wh, "sim_events")}
    assert types["payload"] == "JSON" and types["occurred_at"] == "TIMESTAMP"
    assert types["event_date"] == "DATE" and types["is_synthetic"] == "BOOL"


def test_not_null_becomes_required(wh: Warehouse) -> None:
    modes = {c.name: c.required for c in raw_columns(wh, "sim_events")}
    assert modes["event_id"] is True and modes["causation_id"] is False


def test_unknown_table_and_unmapped_type_are_refused(wh: Warehouse) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        raw_columns(wh, "nope")
    wh.con.execute("CREATE TABLE raw.tmp_odd (x HUGEINT)")
    try:
        with pytest.raises(ValueError, match="no BigQuery mapping"):
            raw_columns(wh, "tmp_odd")
    finally:
        wh.con.execute("DROP TABLE raw.tmp_odd")


def test_export_writes_every_row_with_utc_timestamps_and_nested_json(
    wh: Warehouse, tmp_path: Path
) -> None:
    columns = raw_columns(wh, "sim_events")
    path = tmp_path / "e.ndjson.gz"
    count = export_ndjson_gz(wh, "sim_events", columns, path)
    rows = [json.loads(x) for x in gzip.decompress(path.read_bytes()).decode().splitlines()]
    assert count == len(rows) == wh.count("raw.sim_events")
    assert rows[0]["occurred_at"].endswith("+00:00") and rows[0]["occurred_at"][10] == " "
    assert isinstance(rows[0]["payload"], dict)  # JSON column stays a JSON value
    with pytest.raises(ValueError):
        export_ndjson_gz(wh, "ops.schema_migrations", columns, path)


def test_load_raw_creates_datasets_and_loads_every_table_idempotently(
    wh: Warehouse, tmp_path: Path
) -> None:
    client = FakeClient()
    results = _loader(client).load_raw(wh, tmp_path)
    assert [d.dataset_id for d in client.datasets] == [
        "praxis_dev_raw",
        "praxis_dev_staging",
        "praxis_dev_marts",
    ]
    assert all(
        d.location == "europe-west2" and d.labels["managed_by"] == "praxis" for d in client.datasets
    )
    assert [r.table for r in results] == list(RAW_TABLES)
    for result, load in zip(results, client.loads, strict=True):
        assert load["destination"] == f"praxis-dev-test.praxis_dev_raw.{result.table}"
        assert result.exported_rows == result.loaded_rows == wh.count(f"raw.{result.table}")
        config = load["config"]
        assert config.write_disposition == bigquery.WriteDisposition.WRITE_TRUNCATE
        assert config.source_format == bigquery.SourceFormat.NEWLINE_DELIMITED_JSON
    by_table = {load["destination"].rsplit(".", 1)[1]: load["config"] for load in client.loads}
    for table, (field_name, cluster) in PARTITIONING.items():
        assert by_table[table].time_partitioning.field == field_name
        assert by_table[table].clustering_fields == cluster
    assert by_table["ingest_batches"].time_partitioning is None


def test_row_count_mismatch_fails_loudly(wh: Warehouse, tmp_path: Path) -> None:
    with pytest.raises(LoadMismatchError, match="exported"):
        _loader(FakeClient(drop_rows=1)).load_table(wh, "sim_events", tmp_path)


def test_dry_run_uses_dry_run_and_no_cache() -> None:
    seen: dict[str, Any] = {}

    class DryClient:
        def query(self, sql: str, job_config: Any, location: str, timeout: float) -> FakeJob:
            seen["config"] = job_config
            return FakeJob(output_rows=None, total_bytes_processed=1234)

    loader = BigQueryLoader(DryClient(), project="p", prefix="x_", location="EU")  # type: ignore[arg-type]
    assert loader.dry_run_bytes("select 1") == 1234
    assert seen["config"].dry_run is True and seen["config"].use_query_cache is False


def test_rows_beyond_partition_expiration_are_reported(wh: Warehouse, tmp_path: Path) -> None:
    """Fixture data is from January 2026, so a 60-day expiration would drop all of it."""
    no_expiry = _loader(FakeClient()).load_raw(wh, tmp_path / "a")
    assert all(r.expiring_rows == 0 for r in no_expiry)
    sixty = _loader(FakeClient(expiration_ms=60 * 86_400_000)).load_raw(wh, tmp_path / "b")
    by_table = {r.table: r for r in sixty}
    assert by_table["sim_events"].expiring_rows == wh.count("raw.sim_events")
    assert by_table["external_signals"].expiring_rows == wh.count("raw.external_signals")
    assert by_table["ingest_batches"].expiring_rows == 0  # not partitioned, never expires
