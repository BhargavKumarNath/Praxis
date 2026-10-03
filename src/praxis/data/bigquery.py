"""Load the local raw layer into BigQuery and measure queries with dry runs (ADR 0008).

DuckDB stays the development source of truth; this module mirrors its ``raw`` schema into
BigQuery datasets named ``<prefix>raw|staging|marts`` (the Terraform naming), where dbt then
builds staging and marts with ``--target bigquery``.

Design constraints (BigQuery sandbox, CLAUDE.md section 14):
* load jobs only (free, no DML): each table is exported to gzip NDJSON and loaded with
  ``WRITE_TRUNCATE``, so a re-run replaces the table and is idempotent;
* the BigQuery schema is derived from DuckDB's ``information_schema``: one contract, no copy;
* the loaded row count must equal the exported row count, or the load fails loudly.

The client is injected; nothing here touches the network at import time.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from praxis.data.warehouse import Warehouse

if TYPE_CHECKING:
    from google.cloud import bigquery

logger = logging.getLogger(__name__)

LAYERS = ("raw", "staging", "marts")
RAW_TABLES = ("sim_events", "external_signals", "ingest_batches", "region_locations", "sim_batches")
# Large raw tables are date-partitioned and clustered like the facts built from them.
PARTITIONING: dict[str, tuple[str, list[str]]] = {
    "sim_events": ("event_date", ["event_type", "entity_id"]),
    "external_signals": ("observed_date", ["source", "metric"]),
}
DUCKDB_TO_BIGQUERY = {
    "VARCHAR": "STRING",
    "INTEGER": "INT64",
    "BIGINT": "INT64",
    "DOUBLE": "FLOAT64",
    "BOOLEAN": "BOOL",
    "DATE": "DATE",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMP",
    "JSON": "JSON",
}
LABELS = {"managed_by": "praxis", "environment": "dev", "data": "synthetic_and_public"}


class LoadMismatchError(RuntimeError):
    """BigQuery reported a different row count than was exported."""


@dataclass(frozen=True)
class Column:
    name: str
    bq_type: str
    required: bool


@dataclass(frozen=True)
class LoadResult:
    table: str
    exported_rows: int
    loaded_rows: int
    # Rows in partitions older than the dataset's partition expiration (e.g. the sandbox's
    # 60 days). BigQuery accepts them in the load and then drops them; reported, never hidden.
    expiring_rows: int = 0


def raw_columns(wh: Warehouse, table: str) -> list[Column]:
    rows = wh.con.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'raw' AND table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()
    if not rows:
        raise ValueError(f"raw.{table} does not exist")
    columns = []
    for name, duck_type, nullable in rows:
        if duck_type not in DUCKDB_TO_BIGQUERY:
            raise ValueError(f"raw.{table}.{name}: no BigQuery mapping for {duck_type}")
        columns.append(Column(name, DUCKDB_TO_BIGQUERY[duck_type], nullable == "NO"))
    return columns


def export_ndjson_gz(wh: Warehouse, table: str, columns: list[Column], path: Path) -> int:
    """Write ``raw.<table>`` as gzip NDJSON; timestamps as explicit UTC strings."""
    if table not in RAW_TABLES or "'" in str(path):
        raise ValueError("unknown table or unsafe path")
    exprs = [
        f"strftime({c.name}, '%Y-%m-%d %H:%M:%S.%f') || '+00:00' AS {c.name}"
        if c.bq_type == "TIMESTAMP"
        else c.name
        for c in columns
    ]
    row = wh.con.execute(
        f"COPY (SELECT {', '.join(exprs)} FROM raw.{table} ORDER BY {columns[0].name}) "  # noqa: S608
        f"TO '{path}' (FORMAT JSON, COMPRESSION GZIP)"
    ).fetchone()
    return int(row[0]) if row else 0


def _expiring_rows(wh: Warehouse, table: str, expiration_days: int | None) -> int:
    if expiration_days is None or table not in PARTITIONING:
        return 0
    field = PARTITIONING[table][0]
    row = wh.con.execute(
        f"SELECT count(*) FROM raw.{table} "  # noqa: S608
        f"WHERE {field} < current_date - CAST(? AS INTEGER)",
        [expiration_days],
    ).fetchone()
    return int(row[0]) if row else 0


class BigQueryLoader:
    def __init__(
        self,
        client: bigquery.Client,
        *,
        project: str,
        prefix: str,
        location: str,
        timeout_s: float = 300.0,
    ) -> None:
        self._client = client
        self._project = project
        self._prefix = prefix
        self._location = location
        self._timeout_s = timeout_s

    def dataset_id(self, layer: str) -> str:
        return f"{self._project}.{self._prefix}{layer}"

    def ensure_datasets(self, layers: Iterable[str] = LAYERS) -> list[str]:
        from google.cloud import bigquery

        created = []
        for layer in layers:
            dataset = bigquery.Dataset(self.dataset_id(layer))
            dataset.location = self._location
            dataset.labels = dict(LABELS)
            self._client.create_dataset(dataset, exists_ok=True, timeout=self._timeout_s)
            created.append(dataset.dataset_id)
        return created

    def job_config(self, table: str, columns: list[Column]) -> bigquery.LoadJobConfig:
        from google.cloud import bigquery

        config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            schema=[
                bigquery.SchemaField(
                    c.name, c.bq_type, mode="REQUIRED" if c.required else "NULLABLE"
                )
                for c in columns
            ],
            labels=dict(LABELS),
        )
        if table in PARTITIONING:
            field, cluster = PARTITIONING[table]
            config.time_partitioning = bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY, field=field
            )
            config.clustering_fields = cluster
        return config

    def partition_expiration_days(self) -> int | None:
        dataset = self._client.get_dataset(self.dataset_id("raw"), timeout=self._timeout_s)
        ms = dataset.default_partition_expiration_ms
        return int(ms) // 86_400_000 if ms else None

    def load_table(
        self, wh: Warehouse, table: str, workdir: Path, expiration_days: int | None = None
    ) -> LoadResult:
        columns = raw_columns(wh, table)
        path = workdir / f"{table}.ndjson.gz"
        exported = export_ndjson_gz(wh, table, columns, path)
        destination = f"{self.dataset_id('raw')}.{table}"
        with path.open("rb") as fh:
            job = self._client.load_table_from_file(
                fh, destination, job_config=self.job_config(table, columns), location=self._location
            )
        job.result(timeout=self._timeout_s)
        loaded = int(job.output_rows or 0)
        logger.info(
            "bigquery table loaded",
            extra={"table": destination, "exported_rows": exported, "loaded_rows": loaded},
        )
        if loaded != exported:
            raise LoadMismatchError(f"{destination}: exported {exported}, loaded {loaded}")
        expiring = _expiring_rows(wh, table, expiration_days)
        if expiring:
            logger.warning(
                "rows older than partition expiration will be dropped by BigQuery",
                extra={"table": destination, "expiring_rows": expiring, "days": expiration_days},
            )
        return LoadResult(table, exported, loaded, expiring)

    def load_raw(self, wh: Warehouse, workdir: Path) -> list[LoadResult]:
        workdir.mkdir(parents=True, exist_ok=True)
        self.ensure_datasets()
        days = self.partition_expiration_days()
        return [self.load_table(wh, table, workdir, days) for table in RAW_TABLES]

    def dry_run_bytes(self, sql: str) -> int:
        """Bytes BigQuery would process (after partition pruning). Free; runs nothing."""
        from google.cloud import bigquery

        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
            location=self._location,
            timeout=self._timeout_s,
        )
        return int(job.total_bytes_processed or 0)
