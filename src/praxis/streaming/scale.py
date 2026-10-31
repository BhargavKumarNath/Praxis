"""Scale harness (Phase 14, ADR 0022): archive once, replay through K parallel shards.

Two workloads, both SYNTHETIC and local:

* ``build_archive`` (W1): run the deterministic simulator once, write every event to one of
  ``shards`` gzip NDJSON files (the GCS archive stand-in) and compute the oracle on the way.
  The simulator emits days in non-decreasing order, so a stable sort inside each day equals
  the global stable sort of ``pipeline.expected_state``; a day going backwards raises.
* ``run_scale`` (W2): K processes, one per shard, each replaying its file through the
  production consumers (operational -> Postgres, warehouse -> its own DuckDB file,
  monitoring, DLQ) on its own seeded in-memory broker with chaos faults, all against ONE
  shared Postgres. Events are assigned to shards by ``event_id`` hash (as Pub/Sub spreads
  messages over subscriber instances), so the events of one customer or invoice really are
  applied concurrently by different processes.

Correctness is checked, not assumed: the final Postgres state must equal the oracle, every
customer's subscription must agree with its lifecycle state, and no dead letter may stay
open after the per-shard redrive.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import threading
import time
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import timedelta
from multiprocessing import get_context
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from praxis.control.db import make_engine
from praxis.control.metric_snapshots import publish_batch
from praxis.control.store import ControlPlaneStore, snapshot_checksum
from praxis.data.warehouse import Warehouse
from praxis.events.codec import canonical_bytes
from praxis.simulator.config import load_config
from praxis.simulator.engine import Engine as SimEngine
from praxis.simulator.population import generate_population
from praxis.simulator.runner import peak_rss_mb
from praxis.simulator.validation import StreamValidator
from praxis.streaming.faults import CrashPlan
from praxis.streaming.memory import FaultPlan
from praxis.streaming.pipeline import (
    LocalPipeline,
    observed_state,
    oracle_state,
    subscription_consistent,
)
from praxis.streaming.redrive import redrive_dead_letters
from praxis.streaming.topology import OPERATIONAL, WAREHOUSE, build_topology

MANIFEST = "manifest.json"
ORACLE = "oracle.json.gz"
_REDRIVE_PAGE = 1000


@dataclass(frozen=True)
class ScaleRun:
    """One scale workload. Recorded verbatim in every report."""

    customers: int
    days: int
    sim_seed: int
    fault_seed: int
    shards: int
    chunk: int = 5000
    batch_size: int = 100
    batch_wait_s: float = 1.0
    duplicate_rate: float = 0.1
    max_delay_s: float = 600.0
    reorder: bool = True
    crash_rate: float = 0.005
    pg_pool_size: int = 2
    oracle_lean: bool = False  # True: W1 oracle without id set / non-state events (1M memory)
    duckdb_memory_limit_mb: int | None = None  # per shard; None = DuckDB default (80% of RAM)
    duckdb_threads: int | None = None  # per shard; None = DuckDB default (all cores)

    def __post_init__(self) -> None:
        if self.customers < 1 or self.days < 1 or self.shards < 1:
            raise ValueError("customers, days and shards must be >= 1")


def shard_of(event_id: str, shards: int) -> int:
    digest = hashlib.blake2b(event_id.encode(), digest_size=8).digest()
    return int.from_bytes(digest) % shards


def shard_file(archive: Path, shard: int) -> Path:
    return archive / f"shard-{shard:02d}.ndjson.gz"


def shard_prefix(shard: int) -> str:
    """Subscription-name prefix of one shard (its own topology environment)."""
    return f"praxis-{shard_environment(shard)}-"


def shard_environment(shard: int) -> str:
    return f"scale-s{shard:02d}"


# --- W1: simulate -> sharded archive + oracle ------------------------------------------
# The only event types that change the oracle's customer / invoice state (StreamValidator).
STATE_EVENT_TYPES = frozenset(
    {
        "customer.created", "conversion.observed", "subscription.started",
        "subscription.changed", "churn.observed", "invoice.created", "payment.attempted",
        "payment.succeeded", "payment.failed",
    }
)  # fmt: skip


class _DayOracle:
    """Feeds the validator one day at a time, each day stably sorted by ``occurred_at``.

    ``lean`` keeps no event-id set and buffers only ``STATE_EVENT_TYPES``: a stable sort of a
    subset keeps that subset's relative order, so the final state is identical, but the
    validator's checks on the other events (and on duplicate ids / causation) are skipped.
    """

    def __init__(self, max_attempts: int, *, lean: bool = False) -> None:
        self.validator = StreamValidator(max_attempts, schema_every=1_000_000, track_ids=not lean)
        self._lean = lean
        self._day = ""
        self._buf: list[dict[str, Any]] = []

    def add(self, event: dict[str, Any]) -> None:
        day = str(event["occurred_at"])[:10]
        if day < self._day:
            raise ValueError(f"simulator day went backwards: {day} after {self._day}")
        if day != self._day:
            self.flush()
            self._day = day
        if not self._lean or event["event_type"] in STATE_EVENT_TYPES:
            self._buf.append(event)

    def flush(self) -> None:
        for event in sorted(self._buf, key=lambda e: str(e["occurred_at"])):
            self.validator.feed(event)
        self._buf = []


def build_archive(run: ScaleRun, out: Path) -> dict[str, Any]:
    """Simulate once; write the sharded archive, the oracle and a manifest. Returns W1 report."""
    out.mkdir(parents=True, exist_ok=True)
    cfg = load_config().with_overrides(n_customers=run.customers, days=run.days)
    started, cpu0 = time.perf_counter(), time.process_time()
    population = generate_population(cfg, run.sim_seed)
    oracle = _DayOracle(cfg.billing.max_attempts, lean=run.oracle_lean)
    counts = [0] * run.shards
    with ExitStack() as stack:
        files = [
            stack.enter_context(gzip.open(shard_file(out, i), "wb", compresslevel=1))
            for i in range(run.shards)
        ]
        for event in SimEngine(cfg, run.sim_seed, population).run():
            shard = shard_of(str(event["event_id"]), run.shards)
            files[shard].write(canonical_bytes(event) + b"\n")
            counts[shard] += 1
            oracle.add(event)
    oracle.flush()
    expected = oracle_state(oracle.validator)
    elapsed = time.perf_counter() - started
    with gzip.open(out / ORACLE, "wb", compresslevel=1) as oracle_fh:
        oracle_fh.write(json.dumps(expected, sort_keys=True).encode())
    report = {
        "is_synthetic": True,
        "workload": "W1 simulate + shard archive + oracle",
        "run": asdict(run),
        "events": sum(counts),
        "events_per_shard": counts,
        "elapsed_s": round(elapsed, 3),
        "cpu_s": round(time.process_time() - cpu0, 3),
        "events_per_s": round(sum(counts) / elapsed, 1) if elapsed else None,
        "peak_rss_mb": round(peak_rss_mb(), 1),
        "archive_bytes": sum(shard_file(out, i).stat().st_size for i in range(run.shards)),
        "oracle_checksum": snapshot_checksum(expected),
        "oracle_customers": len(expected["customers"]),
        "oracle_invoices": len(expected["invoices"]),
        "oracle_ledger_entries": expected["ledger_entries"],
        "oracle_ledger_total_minor": expected["ledger_total_minor"],
    }
    (out / MANIFEST).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def read_shard(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rb") as fh:
        for line in fh:
            yield json.loads(line)


# --- W2: one shard ----------------------------------------------------------------------
def _pipeline(run: ScaleRun, store: ControlPlaneStore, wh: Warehouse, shard: int) -> LocalPipeline:
    seed = run.fault_seed * 1000 + shard
    crash = CrashPlan(seed=seed, before_rate=run.crash_rate, after_rate=run.crash_rate)
    return LocalPipeline(
        store,
        wh,
        topology=build_topology(environment=shard_environment(shard)),
        faults=FaultPlan(
            seed=seed,
            duplicate_rate=run.duplicate_rate,
            max_delay_s=run.max_delay_s,
            reorder=run.reorder,
        ),
        crash_plans={OPERATIONAL: crash, WAREHOUSE: crash} if run.crash_rate else {},
        batch_size=run.batch_size,
        batch_wait_s=run.batch_wait_s,
    )


def _redrive_shard(store: ControlPlaneStore, pipeline: LocalPipeline, shard: int) -> int:
    """Operator redrive of this shard's dead letters, page by page, until none is left."""
    total = 0
    while True:
        report = redrive_dead_letters(
            store,
            pipeline.broker,
            pipeline.topology.events_topic,
            limit=_REDRIVE_PAGE,
            source_prefix=shard_prefix(shard),
        )
        total += report.republished
        pipeline.drain()
        if report.republished == 0:  # undecodable letters are never marked: stop, stay open
            return total


def _latency_samples(pipeline: LocalPipeline) -> dict[str, dict[str, Any]]:
    """Reservoirs per role and kind, so the parent can merge shards (not just quantiles)."""
    out: dict[str, dict[str, Any]] = {}
    for role in pipeline.roles:
        per_sub = pipeline.metrics.latency.get(pipeline.subscription(role), {})
        out[role] = {k: {"count": v.count, "values": v.values} for k, v in per_sub.items()}
    return out


def run_shard(
    run: ScaleRun, archive: Path, shard: int, db_url: str, wh_dir: Path
) -> dict[str, Any]:
    """Replay one shard through the consumers (runs in its own process)."""
    os.environ.setdefault("PRAXIS_LOG_LEVEL", "ERROR")
    engine = make_engine(db_url, pool_size=run.pg_pool_size)
    store = ControlPlaneStore(engine)
    wh_dir.mkdir(parents=True, exist_ok=True)
    with Warehouse(wh_dir / f"shard-{shard:02d}.duckdb") as wh:
        if run.duckdb_memory_limit_mb is not None:  # K writers share one host (ADR 0022)
            wh.con.execute(f"SET memory_limit = '{int(run.duckdb_memory_limit_mb)}MB'")
        if run.duckdb_threads is not None:  # one small-batch writer: threads only add overhead
            wh.con.execute(f"SET threads = {int(run.duckdb_threads)}")
        wh.migrate()
        pipeline = _pipeline(run, store, wh, shard)
        started, cpu0 = time.perf_counter(), time.process_time()
        published = pipeline.run(read_shard(shard_file(archive, shard)), chunk=run.chunk)
        elapsed = time.perf_counter() - started
        redriven = _redrive_shard(store, pipeline, shard)
        report = {
            "shard": shard,
            "events_published": published,
            "elapsed_s": round(elapsed, 3),
            "elapsed_with_redrive_s": round(time.perf_counter() - started, 3),
            "cpu_s": round(time.process_time() - cpu0, 3),
            "peak_rss_mb": round(peak_rss_mb(), 1),
            "dead_letters_redriven": redriven,
            "warehouse_rows": wh.count("raw.sim_events"),
            "drain": {k: vars(v) for k, v in pipeline.reports.items()},
            "counters": {
                role: dict(pipeline.metrics.counters.get(pipeline.subscription(role), {}))
                for role in pipeline.roles
            },
            "latency": _latency_samples(pipeline),
        }
    publish_batch(engine, f"streaming-{shard_environment(shard)}", ttl=timedelta(hours=1))
    engine.dispose()
    return report


# --- W2: orchestration, resource sampling, verification ----------------------------------
_PG_STATS = text(
    "SELECT xact_commit, xact_rollback, tup_inserted, tup_updated, tup_deleted, tup_fetched, "
    "blks_read, blks_hit, deadlocks, temp_bytes, pg_database_size(current_database()) "
    "FROM pg_stat_database WHERE datname = current_database()"
)
_PG_FIELDS = (
    "xact_commit", "xact_rollback", "tup_inserted", "tup_updated", "tup_deleted", "tup_fetched",
    "blks_read", "blks_hit", "deadlocks", "temp_bytes", "db_size_bytes",
)  # fmt: skip
_PG_ACTIVITY = text(
    "SELECT count(*), count(*) FILTER (WHERE wait_event_type = 'Lock') "
    "FROM pg_stat_activity WHERE datname = current_database()"
)


def pg_stats(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        row = conn.execute(_PG_STATS).one()
    return {k: int(v) for k, v in zip(_PG_FIELDS, row, strict=True)}


def _mem_used_mb() -> float:
    info: dict[str, int] = {}
    with Path("/proc/meminfo").open() as fh:
        for line in fh:
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0])
    return (info["MemTotal"] - info["MemAvailable"]) / 1024


class ResourceSampler:
    """Background sampler: Postgres connections and lock waits, host memory and load."""

    def __init__(self, engine: Engine, interval_s: float = 5.0) -> None:
        self._engine = engine
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="scale-sampler", daemon=True)
        self.samples: list[dict[str, float]] = []

    def __enter__(self) -> ResourceSampler:
        self.baseline_mem_mb = _mem_used_mb()
        self.samples.append(self.sample())
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()
        self.samples.append(self.sample())

    def sample(self) -> dict[str, float]:
        with self._engine.connect() as conn:
            conns, lock_waits = conn.execute(_PG_ACTIVITY).one()
        return {
            "t": time.time(),
            "pg_connections": float(conns),
            "pg_lock_waits": float(lock_waits),
            "mem_used_mb": _mem_used_mb(),
            "load1": os.getloadavg()[0],
        }

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.samples.append(self.sample())

    def summary(self) -> dict[str, float | int | None]:
        def peak(key: str) -> float | None:
            return max((s[key] for s in self.samples), default=None)

        mem = peak("mem_used_mb")
        return {
            "samples": len(self.samples),
            "pg_connections_max": peak("pg_connections"),
            "pg_lock_waits_max": peak("pg_lock_waits"),
            "host_mem_used_baseline_mb": round(self.baseline_mem_mb, 1),
            "host_mem_used_peak_mb": None if mem is None else round(mem, 1),
            "load1_max": peak("load1"),
        }


def weighted_quantiles(parts: Sequence[Mapping[str, Any]]) -> dict[str, float | int | None]:
    """p50/p95/p99 over shard reservoirs, each value weighted by its shard's count / size."""
    weighted = sorted(
        (v, p["count"] / len(p["values"])) for p in parts if p["values"] for v in p["values"]
    )
    total = sum(w for _, w in weighted)
    out: dict[str, float | int | None] = {"count": sum(int(p["count"]) for p in parts)}
    for name, q in (("p50_ms", 0.50), ("p95_ms", 0.95), ("p99_ms", 0.99)):
        out[name] = _quantile(weighted, total, q)
    out["max_ms"] = round(weighted[-1][0], 3) if weighted else None
    return out


def _quantile(weighted: Sequence[tuple[float, float]], total: float, q: float) -> float | None:
    if not weighted:
        return None
    target, seen = q * total, 0.0
    for value, weight in weighted:
        seen += weight
        if seen >= target - 1e-9:
            return round(value, 3)
    return round(weighted[-1][0], 3)  # pragma: no cover - float rounding guard


def merge_latency(shards: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    roles = sorted({role for s in shards for role in s["latency"]})
    out: dict[str, dict[str, Any]] = {}
    for role in roles:
        kinds = sorted({k for s in shards for k in s["latency"].get(role, {})})
        out[role] = {
            k: weighted_quantiles(
                [s["latency"][role][k] for s in shards if k in s["latency"].get(role, {})]
            )
            for k in kinds
        }
    return out


def _sum_nested(shards: Sequence[Mapping[str, Any]], key: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for s in shards:
        for role, values in s[key].items():
            bucket = out.setdefault(role, {})
            for name, n in values.items():
                bucket[name] = bucket.get(name, 0) + int(n)
    return {r: dict(sorted(v.items())) for r, v in sorted(out.items())}


def load_manifest(archive: Path, run: ScaleRun) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((archive / MANIFEST).read_text())
    built = manifest["run"]
    for key in ("customers", "days", "sim_seed", "shards"):
        if built[key] != getattr(run, key):
            raise ValueError(
                f"archive was built with {key}={built[key]}, run asks {getattr(run, key)}"
            )
    return manifest


def _verify(store: ControlPlaneStore, archive: Path) -> dict[str, Any]:
    with gzip.open(archive / ORACLE, "rt") as fh:
        expected = json.load(fh)
    snap = store.snapshot()
    observed = observed_state(snap)
    matches = observed == expected
    mismatched = {
        part: sum(1 for k in expected[part] if observed[part].get(k) != expected[part][k])
        + len(set(observed[part]) - set(expected[part]))
        for part in ("customers", "invoices")
    }
    return {
        "matches_oracle": matches,
        "mismatched_entities": mismatched,
        "oracle_checksum": snapshot_checksum(expected),
        "observed_checksum": snapshot_checksum(observed),
        "control_plane_checksum": snapshot_checksum(snap),
        "subscription_inconsistencies": len(subscription_consistent(snap)),
        "customers_projected": len(snap["customers"]),
        "invoices_projected": len(snap["invoices"]),
        "ledger_entries": snap["ledger_entries"],
        "ledger_total_minor": snap["ledger_total_minor"],
        "ledger_matches_oracle": (snap["ledger_entries"], snap["ledger_total_minor"])
        == (expected["ledger_entries"], expected["ledger_total_minor"]),
        "pending": store.pending_summary(),
        "dead_letters_open": store.dead_letter_counts(),
    }


def _delta(after: Mapping[str, int], before: Mapping[str, int]) -> dict[str, int]:
    out = {k: after[k] - before[k] for k in after if k != "db_size_bytes"}
    out["db_size_bytes_before"] = before["db_size_bytes"]
    out["db_size_bytes_after"] = after["db_size_bytes"]
    return out


def _run_shards(run: ScaleRun, archive: Path, db_url: str, wh_dir: Path) -> list[dict[str, Any]]:
    ctx = get_context("spawn")  # no forked DB connections or threads in the children
    with ProcessPoolExecutor(max_workers=run.shards, mp_context=ctx) as pool:
        futures = [
            pool.submit(run_shard, run, archive, i, db_url, wh_dir) for i in range(run.shards)
        ]
        return [f.result() for f in futures]


def run_scale(run: ScaleRun, archive: Path, db_url: str, wh_dir: Path) -> dict[str, Any]:
    """W2: replay the archive through ``run.shards`` parallel shard processes and verify."""
    manifest = load_manifest(archive, run)
    engine = make_engine(db_url, pool_size=2)
    store = ControlPlaneStore(engine)
    before = pg_stats(engine)
    started = time.perf_counter()
    with ResourceSampler(engine) as sampler:
        shards = _run_shards(run, archive, db_url, wh_dir)
    wall = time.perf_counter() - started
    after = pg_stats(engine)
    verify = _verify(store, archive)
    engine.dispose()
    published = sum(s["events_published"] for s in shards)
    return {
        "is_synthetic": True,
        "workload": "W2 sharded replay through the production consumers (memory transport)",
        "run": asdict(run),
        "events_published": published,
        "events_in_archive": manifest["events"],
        "wall_s": round(wall, 3),
        "events_per_s": round(published / wall, 1) if wall else None,
        "shard_elapsed_s_max": max(s["elapsed_s"] for s in shards),
        "cpu_s_total": round(sum(s["cpu_s"] for s in shards), 1),
        "cpu_cores_used_avg": round(sum(s["cpu_s"] for s in shards) / wall, 2) if wall else None,
        "shard_peak_rss_mb_max": max(s["peak_rss_mb"] for s in shards),
        "shard_peak_rss_mb_sum": round(sum(s["peak_rss_mb"] for s in shards), 1),
        "resources": sampler.summary(),
        "postgres": _delta(after, before),
        "warehouse_rows": sum(s["warehouse_rows"] for s in shards),
        "dead_letters_redriven": sum(s["dead_letters_redriven"] for s in shards),
        "drain": _sum_nested(shards, "drain"),
        "counters": _sum_nested(shards, "counters"),
        "latency": merge_latency(shards),
        **verify,
        "shards": [
            {k: v for k, v in s.items() if k not in ("latency", "counters", "drain")}
            for s in shards
        ],
    }


def error_rate(report: Mapping[str, Any]) -> float:
    """Dead letters still open after redrive per published event (0 when none published)."""
    open_letters = sum(report["dead_letters_open"].values())
    published = report["events_published"]
    return open_letters / published if published else 0.0


# --- pre-registered protocol and SLOs (configs/scale/slo.toml) -----------------------------
def load_scale_config(path: Path, scale: str) -> tuple[ScaleRun, dict[str, Any]]:
    """The workload of one ladder step and its SLOs (empty for development steps)."""
    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    step = dict(cfg["scales"][scale])
    slo = dict(step.pop("slo", {}))
    run = ScaleRun(**{**cfg["workload"], **step})
    return run, slo


def _latency(report: Mapping[str, Any], role: str, kind: str, q: str) -> float | None:
    value = report["latency"].get(role, {}).get(kind, {}).get(q)
    return None if value is None else float(value)


def evaluate_slo(report: Mapping[str, Any], slo: Mapping[str, Any]) -> dict[str, Any]:
    """Each pre-registered criterion with its measured value and verdict; a missing
    measurement fails its criterion (no data is never a pass)."""
    measured: dict[str, tuple[float | bool | None, str, Any]] = {
        "matches_oracle": (report["matches_oracle"], "==", True),
        "ledger_matches_oracle": (report["ledger_matches_oracle"], "==", True),
        "subscription_inconsistencies": (report["subscription_inconsistencies"], "<=", 0),
        "error_rate": (error_rate(report), "<=", slo.get("max_error_rate", 0.0)),
        "postgres_deadlocks": (report["postgres"]["deadlocks"], "<=", 0),
    }
    limits = {
        "min_events_per_s": ("events_per_s", ">="),
        "max_wall_s": ("wall_s", "<="),
        "max_shard_rss_mb": ("shard_peak_rss_mb_max", "<="),
    }
    for key, (field_name, op) in limits.items():
        if key in slo:
            measured[field_name] = (report.get(field_name), op, slo[key])
    if "max_host_mem_used_mb" in slo:
        peak = report["resources"]["host_mem_used_peak_mb"]
        measured["host_mem_used_peak_mb"] = (peak, "<=", slo["max_host_mem_used_mb"])
    for role, kind, key in _LATENCY_SLOS:
        if key in slo:
            q = "p99_ms" if "p99" in key else "p95_ms"
            measured[key.removeprefix("max_")] = (_latency(report, role, kind, q), "<=", slo[key])
    checks = {name: _check(value, op, limit) for name, (value, op, limit) in measured.items()}
    return {"passed": all(c["pass"] for c in checks.values()), "checks": checks}


_LATENCY_SLOS = (
    (OPERATIONAL, "processing", "max_operational_processing_p99_ms"),
    (OPERATIONAL, "end_to_end", "max_operational_end_to_end_p95_ms"),
    (OPERATIONAL, "end_to_end", "max_operational_end_to_end_p99_ms"),
)


def _check(value: float | bool | None, op: str, limit: Any) -> dict[str, Any]:
    if value is None:
        ok = False
    elif op == "==":
        ok = value == limit
    elif op == ">=":
        ok = value >= limit
    else:
        ok = value <= limit
    return {"value": value, "op": op, "limit": limit, "pass": bool(ok)}


def evaluate_http(summary: Mapping[str, Any], slo: Mapping[str, Any]) -> dict[str, Any]:
    """W3: per-surface p95 / p99 and failure rate from a k6 ``--summary-export`` file.

    A surface with no requests fails (no data is never a pass)."""
    metrics = summary["metrics"]
    checks: dict[str, Any] = {}
    for surface in ("api", "console"):
        duration = metrics.get(f"http_req_duration{{surface:{surface}}}", {})
        failed = metrics.get(f"http_req_failed{{surface:{surface}}}", {})
        checks[f"{surface}_p95_ms"] = _check(duration.get("p(95)"), "<=", slo["max_p95_ms"])
        checks[f"{surface}_p99_ms"] = _check(duration.get("p(99)"), "<=", slo["max_p99_ms"])
        checks[f"{surface}_failed_rate"] = _check(failed.get("value"), "<=", slo["max_failed_rate"])
    dropped = metrics.get("dropped_iterations", {}).get("count", 0)
    checks["dropped_iterations"] = _check(dropped, "<=", 0)
    return {"passed": all(c["pass"] for c in checks.values()), "checks": checks}
