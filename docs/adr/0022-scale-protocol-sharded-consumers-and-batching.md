# ADR 0022: Scale protocol, sharded consumers, micro-batching and telemetry aggregation

Status: Accepted (2026-10-31, Phase 14). Amends ADR 0009 (adds a micro-batch window to the
in-memory drain), ADR 0018 (counter aggregation before export) and the Phase 3 `run-local`
defaults (`--batch-wait 1.0`). No contract in `pyproject.toml` changed; no new dependency.

## Context
Phase 14 must show, with measurements, how far the architecture scales on one machine (16
cores, 14.4 GB RAM, ~260 GB free disk) at GBP 0, and name every bottleneck. Starting points:
10K x 28 d = 977,754 events through `run-local` took 31 min (518 ev/s, 1.8 GB); telemetry
cost ~32% of chaos throughput. Profiling (cProfile; py-spy cannot attach here) showed:

1. **Warehouse trickle.** Above ~1K customers the warehouse consumer wrote 1-3 rows per
   DuckDB insert (163,485 inserts at 3K vs 2,208 at 1K). Processing takes no virtual time in
   the in-memory broker, so when nothing was ready the drain jumped to the *next single*
   message. A fixed ~4-7 ms per insert made the run superlinear. At 1K an earlier
   operational drain happened to advance the clock first, hiding it.
2. **One OpenTelemetry `Counter.add` per delivery** (~25% of profiled time at 1K).
3. **Postgres commit latency**: each stateful event is one transaction (~11 round trips,
   synchronous commit). One writer is commit-bound (~2-4 ms per transaction).
4. **Memory**: `run-local` materialises every event (and the oracle sorts them all), so
   100K (~9.7M events) would not fit in RAM.

## Decision
* **Scale protocol** (`praxis.streaming.scale`, `configs/scale/slo.toml`, pinned by
  `tests/scale/test_config.py`): W1 simulates once into a sharded gzip NDJSON archive (the
  GCS stand-in) and computes the oracle *streaming*: the simulator's days never go
  backwards, so a stable sort within each day equals the global stable sort (checked;
  a regression raises). W2 replays the archive through K shard processes, each with its own
  seeded in-memory broker, chaos faults, consumers and DuckDB file, against ONE Postgres.
  Shards are chosen by `event_id` hash, as Pub/Sub spreads messages over subscriber
  instances, so the events of one customer or invoice are applied concurrently by
  different processes: the Postgres idempotency and advisory locks (ADR 0009) are tested
  under real contention. After the run, every shard redrives its own dead letters
  (`source_prefix` filter, paged until none is left). The final Postgres state must equal
  the oracle; subscription consistency, ledger totals, open dead letters and deadlocks are
  always checked. W3 is k6 (pinned Docker image, no Python dependency) against the API and
  console read surfaces. W4 is the SyntheticPaymentGateway path (`python -m praxis.payments
  load`) with every notification delivered twice. Stripe and public APIs are never loaded.
* **SLOs before runs.** Thresholds come from operational needs (full 28-day replay within
  1 h at 100K and 6 h at 1M; one control-plane apply p99 <= 50 ms against the 5 s lag SLO;
  memory within the host), written and hash-pinned before the first 100K run. Correctness
  criteria are not configurable.
* **Consumer concurrency = shard processes, not threads.** The Python consumers are
  CPU-bound (decode, validation, DuckDB, telemetry); K processes use K cores and K small
  Postgres pools (`pg_pool_size = 2`). Each shard caps its DuckDB buffer pool
  (`duckdb_memory_limit_mb = 512`), because DuckDB defaults to 80% of host RAM per process.
* **Micro-batch window for batch sinks** (`drain(batch_wait_s)`, `run-local --batch-wait`,
  default 1.0 virtual s): when nothing is ready, a batch worker waits up to the window
  before pulling, like a sink that buffers for a bounded time (BigQuery / Storage Write
  batching, Pub/Sub flow control). 1 s is 20% of the 5 s consumer-lag SLO. Non-batch
  workers ignore it; 0 restores the original schedule (all Phase 3 scenario tests keep it).
* **Counter aggregation** (`StreamMetrics`): increments are buffered and pushed to OTel every
  1,024 increments and before every `Telemetry.snapshot()` (`register_pre_collect`), and on
  finalisation, so a reader never sees a stale or double count (`tests/streaming/
  test_metrics_aggregation.py`). Spans and the lag histogram stay per delivery.
* **Rejected**: a multi-row parameterised `VALUES` insert for the warehouse (1.8x slower
  than the NDJSON `read_json` path at 100 rows); `synchronous_commit = off` (it would trade
  away durability of financial state for throughput); larger pull batches for the warehouse
  under chaos (a crash re-delivers the whole batch, so waste grows with batch size).

## Cloud mapping (analysis only; nothing provisioned)
* Cloud Run consumers: one instance ~ one shard process. Concurrency per instance should
  stay low (1-4) for these CPU-bound handlers; scale out with `max-instances` instead, and
  size the Postgres connection budget as `instances x pool` (8 x 2 = 16 here).
* Pub/Sub: streaming pull with flow control (`max_messages` ~ batch size, `max_bytes`) per
  instance plays the role of the batch window; ordering keys are not needed (ADR 0009).
* BigQuery: partition the event fact by `event_date`, cluster by `event_type, entity_id`;
  representative scanned bytes were not measured (sandbox use needs owner approval).

## Consequences
* Scale numbers are for the in-memory transport plus real Postgres and DuckDB on one host;
  they bound the consumers and the control plane, not Pub/Sub or Cloud Run.
* The archive is gzip NDJSON per shard (stdlib); Parquet export remains the warehouse's
  `export_partitioned_parquet`.
* `run-local` defaults changed (`--batch-wait 1.0`), so its drain counts differ from Phase 3
  reports; its oracle and checksums do not (measured).

## Amendment (2026-10-31, after the 100K step passed)
* Two per-step knobs, measured on development steps before use and recorded on `[scales.1m]`
  as changes since 100K: `duckdb_threads` (1 per shard: 10K 8-shard 8,340 -> 10,916 ev/s)
  and `oracle_lean` (W1 oracle without the event-id set, buffering only state-changing event
  types: 100K W1 peak 1,757 -> 790 MB, identical oracle checksum; the validator's
  duplicate-id, causation and non-state checks are skipped in that W1).
* The config pin covers the workload, HTTP / payment SLOs and the gated steps' customers and
  SLOs; development steps may be added without re-pinning.
* **Open limit:** the local warehouse deduplicates through an in-memory primary-key index
  (~180 MB per million rows, OOM at ~5-6M rows per shard under a 512 MB cap). This blocks 1M
  (~97M events) on a 14 GiB host. Options (owner decision, new ADR): partition the fact by
  event date so deduplication stays within a bounded index; drop the key and deduplicate by
  compaction (eventual idempotency); or a larger host / BigQuery.
* At 100K the warehouse micro-batches shrank to ~7 rows per round (74.5 at 10K); within SLO,
  but the first throughput item for a later 1M attempt.
