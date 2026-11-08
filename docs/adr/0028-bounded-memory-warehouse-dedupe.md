# ADR 0028: Warehouse dedupe without an in-memory index; the M1-budget scale ladder

Status: Accepted (2026-11-05, owner-delegated: "evaluate warehouse architecture choices";
"find the maximum technically defensible scale" on an 8 GB Apple M1). Supersedes ADR 0025
amendment 1 (lookup + plain INSERT into the primary-key table) and the event-table part of
ADR 0008 / 0009 (primary key on `raw.sim_events.event_id`). Evidence:
`docs/evidence/final-scale.md`.

## Context
Phase 14 stopped before 1M. DuckDB's ART primary-key index on `raw.sim_events.event_id`
lives in memory, at about 180 MB per million rows, and it cannot be spilled. At 1M customers
there are about 97M events, so the index needs about 17 GB. The measured result was an OOM at
4.9 to 6.1M rows per shard with a 512 MB memory_limit. The index existed for one reason:
exactly-once rows under at-least-once delivery (duplicates, redeliveries, crashes and
replays).

Profiling a 10K shard (121K events) also showed where the rest of the time goes:
* about 86 s in 21K warehouse writes;
* 22 s waiting on Postgres;
* about 30 s in pydantic, uuid, json and OTel.

Each file-database write commits, and a commit costs about 1.2 ms (fdatasync on this NVMe).
The chaos crash rate keeps the crash-isolating lease windows (ADR 0025) near 10 messages, and
a longer batch window does not change that: `batch_wait_s` 1 s and 10 s gave the same number
of rounds.

## Options
1. Keep the PK and shard harder. Memory per shard is unchanged, so this only moves the wall.
2. A Bloom filter or hash set of seen ids in Python. This is still O(events) memory, and it
   loses exactness on restart unless it is persisted.
3. Partition the PK table per day. Each partition index is smaller, but DuckDB keeps every
   attached index in memory, so the total is unchanged.
4. **Append-only landing table plus set-based compaction with a date-scoped anti-join**
   (chosen).
5. Parquet files per batch with dedupe at read time (DuckLake-style). This is a larger
   change to every reader (dbt, forecasting, MLOps), and dedupe at read time is paid by
   every query.

## Decision
* Migration `0002_event_dedupe_without_index.sql` rebuilds `raw.sim_events` with the same
  columns and NOT NULLs but **no PRIMARY KEY**. It adds `raw.sim_events_landing` with a
  sequence `seq`.
* `Warehouse.insert_events` drops duplicates within the batch and appends the batch to
  landing. That append is a durable commit, so the message can be acked. Once at least
  `compact_every` landed rows are waiting (0 means every write), `_compact` runs one
  transaction. It inserts the first-landed row per `event_id` that is not already in
  `sim_events` for the same `event_date` (an anti-join restricted to the landed date range),
  in `(event_date, occurred_at)` order, then empties landing. On error it rolls back, so
  landing keeps the rows for the next compaction.
* `load_sim_events` (the batch loader) uses the same landing and compaction path. Streaming
  and batch therefore cannot drift: a Hypothesis test asserts that they give the same table
  for every `compact_every`, including first-occurrence semantics
  (`tests/data/test_event_insert_paths.py`).
* `event_id` is a function of `event_date`: the simulator derives ids from content, and
  `occurred_at` never changes on redelivery. A duplicate therefore always has the same
  `event_date`, and the date-scoped anti-join is exact. `Warehouse.duplicate_event_ids()`
  checks the invariant directly, and the scale run reports it and gates it at 0.
* `Warehouse.flush()` compacts whatever is pending. Readers that need every row (the scale
  run's verification, dbt) call it, or open the file after the writer closed it. A
  per-file lock serialises compaction.

## Memory and cost
Memory no longer grows with history. An anti-join over a date range is a hash join whose
build side is that range's landed rows, and DuckDB can spill it under `memory_limit`.
Compaction is O(landed rows + rows on those dates). On a replay, rows arrive in date order, so
that is the current few days. Small-batch landing still costs one commit per write. That is
the price of an ack only after a durable write, which is unchanged.

## The M1-budget ladder
`configs/scale/slo.toml` gains steps `m1-100k`, `m1-250k`, `m1-500k` and `m1-1m`, registered
before any run and pinned by `tests/scale/test_config.py`. The Phase 14 registration is
unchanged and pinned separately. `make scale-m1 SCALE=<step>` runs W1 and W2, each in its own
cgroup v2 scope (`systemd-run --user --scope -p MemoryMax=4096M -p MemorySwapMax=0`) on 8
logical CPUs (`taskset`), with Postgres in a separate container (`--memory 1g`, no swap,
2 CPUs). That is 5 GB of an 8 GB M1, leaving about 3 GB for macOS. Gated checks:
* every correctness check, including warehouse rows equal to archived events and 0
  duplicate ids;
* 0 OOM kills in the scope;
* 0 swap;
* wall time at most 6 h;
* operational p99 at most 50 ms.

Throughput is reported but not gated. `memory.peak` includes page cache and cannot exceed
`memory.max`, so it is reported, not used as the binding check.

**This is an emulation.** CPU speed, the memory compressor, unified-memory behaviour, APFS
and the Apple SSD all differ from a real M1. A real M1 run is the owner's to make (the same
target with a macOS memory cap is not available: `systemd-run` is Linux-only).

## Also changed
* The simulator emits each day's bulk sections (usage and request events, about 80% of all
  events) as column-backed generators merged in `(ts, entity_id, uid)` order. Before, it
  built one `Draft` object per event and sorted the whole day. The event stream is
  byte-identical on four worlds (SHA-256 of every canonical line: development seed 1, and the
  pricing-shadow, recovery and elasticity scenarios). At most 10 products are supported, so
  usage uids order by a one-digit index, and the engine refuses more.
* The scale report adds `cgroup_*` memory accounting, `warehouse_duplicate_ids` and
  `warehouse_bytes`.
* **Disclosed change after m1-250k and before m1-1m; no criterion changed.**
  * **What was measured:** W1 peak RSS grew linearly, at about 4.2 KB per customer: 559 MB at
    100K, 1,148 MB at 250K and 2,128 MB at 500K. That projects to about 4.3 GB at 1M, over the
    4 GB scope.
  * **Cause:** on day 0, every existing customer's arrival, exposure and subscription events
    were held as objects, and the lean oracle buffered the day's state events to sort an
    already ordered stream.
  * **Fixes:**
    * those sections now render from columns, and the event stream is byte-identical on the
      same four worlds;
    * the lean oracle feeds the stream directly and refuses an out-of-order event, giving
      identical 10K and 100K oracle checksums;
    * `snapshot_checksum` hashes the JSON incrementally, giving the same digest (tested).
  * **Result:** W1 at 100K fell from 559 to 307 MB.

## Amendment 1 (2026-11-08): streaming writers compact per threshold, not per micro-batch

* **What was measured:** the first `make nightly-perf` after this ADR **FAILED**. The 1K chaos
  benchmark (`run-local`, 1K customers x 28 days) ran at 575 ev/s against a floor of 1,218
  (baseline 2,436; Phase 16 measured 1,800.9). The simulator side was unchanged (70,770 ev/s).
* **Cause:** `Warehouse` defaults to `compact_every = 0`, so the CLI streaming writers
  (`run-local`, `consume`, `emulator-bench`) compacted after every micro-batch (about 6 rows).
  Each compaction anti-joins against the growing, unindexed `raw.sim_events`, so the total cost
  grows with the square of the stream. The scale ladder never hit this: it already set
  `warehouse_compact_rows = 50000`. An A/B with only the threshold changed gave 1,572.9 ev/s
  and the same oracle match.
* **Fix:** `praxis.data.warehouse.STREAM_COMPACT_ROWS = 50_000` (the value already registered
  for the scale ladder, not tuned on this benchmark) is used by the three CLI streaming
  writers. Each calls `flush()` before it reads counts. The library default stays 0 (a write
  is visible when it returns) for callers that need that. The trade-off: a streaming writer's
  rows are durable in `raw.sim_events_landing` at once but appear in `raw.sim_events` only at
  the next compaction or `flush()`.
* **Tests:** `test_streaming_writers_compact_per_threshold_not_per_micro_batch` pins the
  behaviour. The existing `test_run_local_matches_oracle_and_writes_report` asserts
  `warehouse_rows == events_published` on a world below the threshold, so it fails without
  the flush.
* **Result:** `make nightly-perf` re-run on the fixed code passed at 1,541.9 ev/s (floor 1,218).
  That is still about 14 % below Phase 16. The baseline was **not** re-recorded. Both runs are
  in `docs/evidence/final-release.md`.
