"""Local analytical warehouse (DuckDB) with versioned SQL migrations.

DuckDB stands in for BigQuery during development (ADR 0008). Every write here is idempotent:
signals upsert on ``record_key``, events insert on ``event_id`` and ignore duplicates, batch
rows upsert on ``batch_id``. Re-running any load leaves the logical content unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import duckdb

from praxis.data.config import Location
from praxis.data.models import BatchProvenance, SignalRecord, SourceId

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_SIGNAL_COLUMNS = (
    "record_key, source, series_id, entity_id, metric, unit, observed_at, "
    "CAST(observed_at AS DATE), value, batch_id, retrieved_at, schema_version"
)
_EVENT_FILE_COLUMNS = (
    "{'event_id':'VARCHAR','event_type':'VARCHAR','source':'VARCHAR','schema_version':'INTEGER',"
    "'entity_id':'VARCHAR','occurred_at':'VARCHAR','published_at':'VARCHAR','trace_id':'VARCHAR',"
    "'correlation_id':'VARCHAR','causation_id':'VARCHAR','is_synthetic':'BOOLEAN',"
    "'payload':'JSON'}"
)

_INSERT_EVENTS = (
    "INSERT INTO raw.sim_events "
    "SELECT event_id, event_type, source, schema_version, entity_id, "
    "CAST(occurred_at AS TIMESTAMPTZ), CAST(published_at AS TIMESTAMPTZ), "
    "CAST(CAST(occurred_at AS TIMESTAMPTZ) AS DATE), trace_id, correlation_id, "
    "causation_id, is_synthetic, payload, ?, ? "
    "FROM read_json(?, format='newline_delimited', columns=" + _EVENT_FILE_COLUMNS + ") "
    "ON CONFLICT (event_id) DO NOTHING"
)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


class MigrationError(RuntimeError):
    pass


class Warehouse:
    def __init__(self, path: Path | None = None) -> None:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(path) if path else ":memory:")
        self._con.execute("SET TimeZone = 'UTC'")

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._con.close()

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        return self._con

    # --- migrations -------------------------------------------------------------------
    def migrate(self) -> list[str]:
        """Apply pending migrations in order; refuse if an applied file was edited."""
        self._con.execute("CREATE SCHEMA IF NOT EXISTS ops")
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS ops.schema_migrations ("
            "version VARCHAR PRIMARY KEY, checksum VARCHAR NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL)"
        )
        applied = dict(
            self._con.execute("SELECT version, checksum FROM ops.schema_migrations").fetchall()
        )
        newly: list[str] = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            sql = path.read_text()
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            if path.stem in applied:
                if applied[path.stem] != checksum:
                    raise MigrationError(f"applied migration {path.stem} was modified")
                continue
            self._con.execute("BEGIN")
            try:
                self._con.execute(sql)
                self._con.execute(
                    "INSERT INTO ops.schema_migrations VALUES (?, ?, ?)",
                    [path.stem, checksum, datetime.now(UTC)],
                )
            except Exception:
                self._con.execute("ROLLBACK")
                raise
            self._con.execute("COMMIT")
            newly.append(path.stem)
        return newly

    # --- reference data ---------------------------------------------------------------
    def load_region_locations(self, locations: Iterable[Location]) -> None:
        self._con.execute("DELETE FROM raw.region_locations")
        for loc in locations:
            self._con.execute(
                "INSERT INTO raw.region_locations VALUES (?, ?, ?, ?, ?, ?)",
                [
                    loc.region_id,
                    loc.name,
                    loc.latitude,
                    loc.longitude,
                    loc.carbon_area,
                    loc.eia_respondent,
                ],
            )

    # --- external signals -------------------------------------------------------------
    def record_batch(self, prov: BatchProvenance) -> None:
        row = prov.model_dump(mode="json")
        columns = list(row)
        updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c != "batch_id")
        self._con.execute(
            f"INSERT INTO raw.ingest_batches ({', '.join(columns)}) "  # noqa: S608
            f"VALUES ({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT (batch_id) DO UPDATE SET {updates}",
            [row[c] for c in columns],
        )

    def upsert_signals(self, records: Iterable[SignalRecord]) -> int:
        """Upsert by logical key. Returns the number of NEW logical records."""
        unique = {r.record_key: r for r in records}  # last occurrence wins inside one batch
        if not unique:
            return 0
        before = self.count("raw.external_signals")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "signals.ndjson"
            with path.open("w") as fh:
                for r in unique.values():
                    fh.write(json.dumps(r.model_dump(mode="json")) + "\n")
            self._con.execute(
                f"INSERT INTO raw.external_signals SELECT {_SIGNAL_COLUMNS} "  # noqa: S608
                "FROM read_json(?, format='newline_delimited', columns={"
                "'record_key':'VARCHAR','source':'VARCHAR','series_id':'VARCHAR',"
                "'entity_id':'VARCHAR','metric':'VARCHAR','unit':'VARCHAR',"
                "'observed_at':'TIMESTAMPTZ','value':'DOUBLE','batch_id':'VARCHAR',"
                "'retrieved_at':'TIMESTAMPTZ','schema_version':'INTEGER'}) "
                "ON CONFLICT (record_key) DO UPDATE SET unit = excluded.unit, "
                "value = excluded.value, batch_id = excluded.batch_id, "
                "retrieved_at = excluded.retrieved_at, schema_version = excluded.schema_version "
                "WHERE excluded.retrieved_at >= raw.external_signals.retrieved_at",
                [str(path)],
            )
        return self.count("raw.external_signals") - before

    def latest_observation(self, source: SourceId, now: datetime) -> datetime | None:
        """Newest observation at or before ``now`` (excludes forecast slots in the future)."""
        row = self._con.execute(
            "SELECT epoch(max(observed_at)) FROM raw.external_signals "
            "WHERE source = ? AND observed_at <= ?",
            [source.value, now],
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return datetime.fromtimestamp(float(row[0]), tz=UTC)

    # --- internal (simulator) events --------------------------------------------------
    def load_sim_events(self, ndjson: Path, manifest: dict[str, Any]) -> int:
        """Idempotently load an NDJSON event file. Returns the number of NEW events."""
        batch_id = str(manifest["batch_id"])
        loaded_at = datetime.now(UTC)
        before = self.count("raw.sim_events")
        self._con.execute(
            _INSERT_EVENTS,
            [batch_id, loaded_at, str(ndjson)],
        )
        self._con.execute(
            "INSERT INTO raw.sim_batches VALUES (?, ?, ?) ON CONFLICT (batch_id) DO NOTHING",
            [batch_id, json.dumps(manifest, sort_keys=True), loaded_at],
        )
        return self.count("raw.sim_events") - before

    # --- helpers ----------------------------------------------------------------------
    def count(self, table: str) -> int:
        row = self._con.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
        assert row is not None  # noqa: S101
        return int(row[0])

    def export_partitioned_parquet(self, table: str, out_dir: Path, partition_col: str) -> int:
        """Write ``table`` as Hive-partitioned Parquet (the GCS / BigQuery load layout)."""
        if not _IDENTIFIER.match(table) or not _IDENTIFIER.match(partition_col):
            raise ValueError("table and partition column must be plain identifiers")
        if "'" in str(out_dir):
            raise ValueError("output path must not contain a single quote")
        self._con.execute(
            f"COPY (SELECT * FROM {table}) TO '{out_dir}' "  # noqa: S608
            f"(FORMAT PARQUET, PARTITION_BY ({partition_col}), OVERWRITE_OR_IGNORE)"
        )
        return len(list(out_dir.glob(f"{partition_col}=*")))
