"""Local analytical warehouse (DuckDB) with versioned SQL migrations.

DuckDB stands in for BigQuery during development (ADR 0008). Every write here is idempotent:
signals upsert on ``record_key``, events insert on ``event_id`` and ignore duplicates (ADR
0028: a date-scoped anti-join, not an in-memory index), batch rows upsert on ``batch_id``.
Re-running any load leaves the logical content unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import duckdb

from praxis.data.config import Location
from praxis.data.models import BatchProvenance, QualityStatus, SignalRecord, SourceId

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

# One projection (casts, derived event_date, provenance) for every event write path, so the
# batch loader and the streaming path below can never disagree about an event.
_EVENT_PROJECTION = (
    "SELECT event_id, event_type, source, schema_version, entity_id, "
    "CAST(occurred_at AS TIMESTAMPTZ), CAST(published_at AS TIMESTAMPTZ), "
    "CAST(CAST(occurred_at AS TIMESTAMPTZ) AS DATE), trace_id, correlation_id, "
    "causation_id, is_synthetic, payload, ?, ? "
)
_EVENT_COLUMNS = (
    "event_id, event_type, source, schema_version, entity_id, occurred_at, published_at, "
    "event_date, trace_id, correlation_id, causation_id, is_synthetic, payload, batch_id, "
    "loaded_at"
)
_LAND = "INSERT INTO raw.sim_events_landing (" + _EVENT_COLUMNS + ") "
_EVENT_FILE = "FROM read_json(?, format='newline_delimited', columns=" + _EVENT_FILE_COLUMNS + ")"
# ADR 0028: compaction = first landed occurrence of each id, minus ids already stored. The
# anti-join reads only the stored rows of the landed event dates (an event's date is a pure
# function of the event), which zone maps prune to those days; no index is held in memory.
_COMPACT = (
    "INSERT INTO raw.sim_events (" + _EVENT_COLUMNS + ") "  # noqa: S608 - constant columns
    "SELECT " + _EVENT_COLUMNS + " FROM ("
    "  SELECT *, row_number() OVER (PARTITION BY event_id ORDER BY seq) AS occurrence"
    "  FROM raw.sim_events_landing) AS l "
    "WHERE occurrence = 1 AND NOT EXISTS ("
    "  SELECT 1 FROM raw.sim_events AS e"
    "  WHERE e.event_date BETWEEN ? AND ? AND e.event_date = l.event_date"
    "  AND e.event_id = l.event_id) "
    "ORDER BY event_date, occurred_at"
)
_EVENT_KEYS = ("event_id", "event_type", "source", "schema_version", "entity_id",
               "occurred_at", "published_at", "trace_id", "correlation_id", "causation_id",
               "is_synthetic", "payload")  # fmt: skip
_EVENT_VALUE_ROW = (
    "(?::VARCHAR, ?::VARCHAR, ?::VARCHAR, ?::INTEGER, ?::VARCHAR, ?::VARCHAR, ?::VARCHAR, "
    "?::VARCHAR, ?::VARCHAR, ?::VARCHAR, ?::BOOLEAN, ?::JSON)"
)
# Measured crossover (DuckDB 1.5): bound parameters are cheaper than a temporary NDJSON
# file up to about 12-16 rows; above that, binding cost grows faster than the file scan.
_MAX_BOUND_ROWS = 8
# One lock per warehouse file: DuckDB serialises writers across processes (file lock), this
# serialises connections of one process, so the check-then-insert of a compaction is atomic.
_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


class MigrationError(RuntimeError):
    pass


# ADR 0028 amendment 1: the compaction threshold for long-running streaming writers. Compacting
# after every micro-batch re-scans the growing table each time (quadratic in the stream: the
# 1K chaos benchmark fell from 1,800 to 575 ev/s); 50,000 rows is the value already registered
# for the scale ladder (configs/scale/slo.toml). Writers call ``flush()`` before reading counts.
STREAM_COMPACT_ROWS = 50_000


class Warehouse:
    def __init__(
        self, path: Path | None = None, *, read_only: bool = False, compact_every: int = 0
    ) -> None:
        """``read_only`` (readers such as the ops collectors) never creates the file and
        never takes the writer lock; DuckDB refuses it while a writer holds the file.

        ``compact_every``: streaming writes land and are compacted into ``raw.sim_events``
        once at least this many rows have landed (0 = on every write, so a write is visible
        when it returns). A larger value amortises the compaction over many micro-batches
        (scale replays); landed rows are durable, and ``flush()`` makes them visible."""
        if compact_every < 0:
            raise ValueError("compact_every must be >= 0")
        self._compact_every = compact_every
        self._landed = 0
        key = str(path.resolve()) if path is not None else f":memory:{id(self)}"
        with _WRITE_LOCKS_GUARD:
            self._lock = _WRITE_LOCKS.setdefault(key, threading.Lock())
        if path is not None and not read_only:
            path.parent.mkdir(parents=True, exist_ok=True)
        if read_only:
            if path is None:
                raise ValueError("a read-only warehouse needs a file")
            self._con = duckdb.connect(str(path), read_only=True)
        else:
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

    def quarantined_batches(self, since: datetime, until: datetime) -> dict[str, int]:
        """Batches that failed their contract, by source, retrieved in [since, until)."""
        rows = self._con.execute(
            "SELECT source, count(*) FROM raw.ingest_batches WHERE quality_status = ? "
            "AND retrieved_at >= ? AND retrieved_at < ? GROUP BY source ORDER BY source",
            [QualityStatus.QUARANTINED.value, since, until],
        ).fetchall()
        return {str(source): int(n) for source, n in rows}

    # --- internal (simulator) events --------------------------------------------------
    def load_sim_events(self, ndjson: Path, manifest: dict[str, Any]) -> int:
        """Idempotently load an NDJSON event file. Returns the number of NEW events."""
        batch_id = str(manifest["batch_id"])
        loaded_at = datetime.now(UTC)
        with self._lock:
            self._con.execute(
                _LAND + _EVENT_PROJECTION + _EVENT_FILE, [batch_id, loaded_at, str(ndjson)]
            )
            new = self._compact()
            self._con.execute(
                "INSERT INTO raw.sim_batches VALUES (?, ?, ?) ON CONFLICT (batch_id) DO NOTHING",
                [batch_id, json.dumps(manifest, sort_keys=True), loaded_at],
            )
        return new

    def insert_events(self, events: Iterable[dict[str, Any]], batch_id: str) -> int:
        """Idempotently insert envelope dicts (streaming path). Returns the number of NEW
        events this call made visible in ``raw.sim_events``.

        The rows land in ``raw.sim_events_landing`` (append only, durable when this returns)
        and are compacted once ``compact_every`` rows have landed: the first landed
        occurrence of an id wins and ids already stored are skipped, exactly as
        ``INSERT ... ON CONFLICT (event_id) DO NOTHING`` behaved (ADR 0028). With a deferred
        compaction the call returns 0 and ``flush()`` reports the new events later.
        """
        rows: dict[str, dict[str, Any]] = {}
        for event in events:
            rows.setdefault(str(event["event_id"]), event)
        if not rows:
            return 0
        with self._lock:
            self._land(list(rows.values()), batch_id)
            self._landed += len(rows)
            if self._landed < self._compact_every:
                return 0
            return self._compact()

    def flush(self) -> int:
        """Compact every landed row into ``raw.sim_events``. Returns the NEW event count."""
        with self._lock:
            return self._compact()

    def duplicate_event_ids(self) -> int:
        """Stored rows beyond one per ``event_id`` (the uniqueness invariant: always 0)."""
        row = self._con.execute(
            "SELECT count(*) - count(DISTINCT event_id) FROM raw.sim_events"
        ).fetchone()
        assert row is not None  # noqa: S101
        return int(row[0])

    def _land(self, rows: list[dict[str, Any]], batch_id: str) -> None:
        loaded_at = datetime.now(UTC)
        if len(rows) <= _MAX_BOUND_ROWS:
            params: list[Any] = [batch_id, loaded_at]
            for row in rows:
                params.extend(_event_param(row, key) for key in _EVENT_KEYS)
            values = ", ".join([_EVENT_VALUE_ROW] * len(rows))
            source = f"FROM (VALUES {values}) AS v({', '.join(_EVENT_KEYS)})"
            self._con.execute(_LAND + _EVENT_PROJECTION + source, params)
            return
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.ndjson"
            with path.open("w") as fh:
                for row in rows:
                    fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            self._con.execute(
                _LAND + _EVENT_PROJECTION + _EVENT_FILE, [batch_id, loaded_at, str(path)]
            )

    def _compact(self) -> int:
        """Move landed rows into ``raw.sim_events`` in one transaction (caller holds the lock)."""
        span = self._con.execute(
            "SELECT min(event_date), max(event_date) FROM raw.sim_events_landing"
        ).fetchone()
        self._landed = 0
        if span is None or span[0] is None:
            return 0
        self._con.execute("BEGIN")
        try:
            result = self._con.execute(_COMPACT, [span[0], span[1]]).fetchone()
            self._con.execute("DELETE FROM raw.sim_events_landing")
        except Exception:
            self._con.execute("ROLLBACK")
            raise
        self._con.execute("COMMIT")
        return int(result[0]) if result else 0

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


def _event_param(row: dict[str, Any], key: str) -> Any:
    """A bound parameter typed like the NDJSON column of ``_EVENT_FILE_COLUMNS``."""
    value = row.get(key)
    if key == "payload" and value is not None:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value
