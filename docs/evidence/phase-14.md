# Phase 14 evidence: Scale Engineering

```text
Phase: 14, Scale Engineering
Date: 2026-10-31
Code revision: e09cbb1 + uncommitted Phase 14 working tree (nothing committed or pushed)
Environment: local, one host: 16 logical cores, 14.4 GiB RAM (about 5.2-6.2 GiB already in
             use by the desktop before each run), Linux 7.0, Python 3.12.14, Postgres 17.11
             in Docker (`praxis-pg`), DuckDB 1.5.6, SQLAlchemy 2.1.3, psycopg 3.3.6,
             k6 1.3.0 (pinned image digest). No cloud resources, no Stripe, no public API,
             no LLM. Every customer, event and amount is SYNTHETIC.
```

## Gate summary

| Step | Verdict |
| --- | --- |
| 100K virtual customers | **PASS**: all 10 pre-registered SLO checks; HTTP (W3) and payment (W4) SLOs pass |
| 1M virtual customers | **NOT RUN**: blocked by a measured memory limit on this host (below); owner decision |
| Phase 14 gate | **NOT PASSED** as a whole: the 1M milestone (required_test.md s20) has no measurement |

No scale above 100K is claimed.

## Protocol (ADR 0022)

* Workloads and SLOs live in `configs/scale/slo.toml`. The workload, the customers per step
  and every SLO are hash-pinned by `tests/scale/test_config.py`. The 100K SLOs were written and
  pinned before the first 100K run.
* Seeds: simulator seed 1 (development), fault seed 701 (fresh). No held-out seed was used for
  any scale step. Exception, disclosed: the first optimisation A/B pairs used `run-local`'s
  default seed 42. They are superseded by seed-1 re-measurements (below). No model or held-out
  evaluation ran in any of them.
* W1 `make scale-archive`: simulate once into an 8-shard gzip NDJSON archive and compute the
  oracle (customer, subscription and invoice states and the ledger) while streaming.
* W2 `make scale-run`: 8 shard processes. Each has its own seeded in-memory broker and chaos:
  10% duplicates, delays up to 600 s, reordering, consumer crash rate 0.005, plus the
  OPERATIONAL and WAREHOUSE crash plans. Each also has its own consumers and its own DuckDB
  file. All shards share ONE Postgres. Events are spread by `event_id` hash, so one
  customer's events are applied concurrently by different processes. Dead letters are
  redriven per shard. Correctness (oracle, ledger, subscription consistency, open dead
  letters, deadlocks) is always gated.
* W3 `make scale-http`: k6 constant-arrival-rate on the core API and the console read
  surfaces, against the 100K database.
* W4 `make scale-payments`: `SyntheticPaymentGateway`, with every webhook notification
  delivered twice.

## Results

### 100K (gated step; `phase-14-100k-w1.json`, `phase-14-100k-w2.json`)

| Metric | Value |
| --- | --- |
| Customers / days | 100,000 / 28 (seed 1, fault seed 701) |
| Events | 9,737,517 published; all 9,737,517 in the warehouse; 509,525 operational applies |
| W1 (simulate + archive + oracle) | 248 s, 39.2K ev/s, peak RSS 1,757 MB, archive 886 MB (gzip) |
| W2 duration | 2,166 s (36.1 min); SLO <= 3,600 s |
| W2 throughput | **4,494.6 ev/s**; SLO >= 2,700 |
| Operational apply latency (processing) | p50 4.4 ms, p95 13.9 ms, **p99 21.3 ms** (SLO <= 50 ms), max 349 ms |
| Warehouse insert latency (processing, per delivery) | p50 0.84 ms, p95 1.97 ms, p99 2.98 ms |
| Consumer lag (wall clock, producer send to handling) | operational p50 5.7 s, p95 16.9 s, p99 25.4 s; warehouse p99 19.1 s (not gated, see limitations) |
| Error rate | 0 open dead letters out of 9,737,517 (210 dead letters redriven to success) |
| Correctness | final state equals the oracle (0 mismatched customers or invoices); ledger 4,902,803,039 minor units equals the oracle; 0 subscription inconsistencies; checksum `40600e56...` equals W1 |
| Chaos absorbed | operational 4,700 crashes; warehouse 92,261 crashes, 12.9M deliveries for 10.7M acks |
| CPU | 9.0 cores used on average (of 16); load1 max 30.1 |
| Memory | shard peak RSS max 740 MB, sum 5,733 MB; host used 5,616 -> 10,959 MB (SLO <= 13,000) |
| DB pressure | 10 connections max (8 x pool 2); 0 lock waits sampled; **0 deadlocks**; 1,025,044 commits, 458 rollbacks (advisory-lock / conflict retries); DB 9 MB -> 516 MB; 0 temp bytes; cache hit 98.2% |
| Warehouse volume | 8 DuckDB files, 2.82 GB total (about 290 B per event) |
| Compute | one host, 8 shard processes x 1 Python process each, one Postgres container |
| Cost | GBP 0 (local) |

SLO check (pre-registered, `[scales.100k.slo]`): events_per_s 4,494.6 >= 2,700; wall_s 2,166 <=
3,600; operational processing p99 21.3 <= 50 ms; shard RSS 740 <= 1,500 MB; host memory 10,959
<= 13,000 MB; error rate 0 <= 0; matches oracle; ledger matches oracle; 0 subscription
inconsistencies; 0 deadlocks. **10 / 10 pass.**

**W3 HTTP at 100K** (`phase-14-100k-http.json`; `[http.100k]`: 50 rps per surface for 60 s):
6,001 requests, 0 failed, 0 dropped iterations. API p95 80.6 ms / p99 184.1 ms; console p95
99.5 ms / p99 154.4 ms. SLO p95 <= 250 ms, p99 <= 1,000 ms, failed <= 0.1%. **PASS.**

**W4 payments at 10K** (`phase-14-payments-10k.json`; `[payments.10k]`, seed 701): 10,000
enrolments (981 with failing cards), 40,000 notifications, each delivered twice, 0 duplicate
inbox rows. Ledger 9,019 entries / 44,193,100 minor units equals the gateway's paid invoices.
Enrol p50 10.0 ms, p99 33.8 ms (SLO <= 100 ms), 27.6 enrolments/s, 110.5 notifications/s, peak
RSS 114 MB. **PASS (correct and within SLO).** W4 was not run at 100K: at about 28
enrolments/s a single writer would need about an hour (see bottlenecks).

### 10K (development steps)

| Run | Wall | ev/s | Op p99 | Lag p99 (op) | Shard RSS max | Host peak | Oracle |
| --- | --- | --- | --- | --- | --- | --- | --- |
| W1 (`phase-14-10k-w1.json`) | 21.9 s | 44.5K | - | - | 339 MB | - | 974,034 events, checksum `8a3d03ad...` |
| W2, 1 shard (`-10k-1shard-w2`) | 944.9 s | 1,030.8 | 5.8 ms | 15.8 s | 748 MB | 6,560 MB | match |
| W2, 8 shards, no DuckDB cap (`-nocap-w2`) | 114.5 s | 8,506.7 | 16.6 ms | 5.3 s | 491 MB | 8,600 MB | match |
| W2, 8 shards, cap 512 MB (`-10k-8shards-w2`) | 116.8 s | 8,340.2 | 15.9 ms | 5.4 s | 496 MB | 8,707 MB | match |
| W2, 8 shards, cap 512 MB, DuckDB threads 1 (`-threads1-w2`) | 89.2 s | 10,915.6 | 7.3 ms | 5.2 s | 395 MB | 7,775 MB | match |

Each 10K row is one run (no repeats), so differences of a few percent are within noise.

### 1M: not run

The start prompt allows 1M once 100K meets its SLOs. Two changes since 100K were measured on
development steps and recorded on `[scales.1m]`:

1. `duckdb_threads = 1`: one small-batch writer per shard gains nothing from DuckDB's thread
   pool. Measured: 1.2M-row insert benchmark, 173 s and 406 CPU s down to 136 s and 106 CPU s;
   10K 8-shard W2, 8,340 to 10,916 ev/s, same checksum.
2. `oracle_lean = true`: the W1 oracle keeps no event-id set and buffers only the 9
   state-changing event types. Measured at 100K: peak RSS 1,757 to 790 MB with the identical
   oracle checksum and an identical archive (decompressed shard md5 equal). Its cost is
   disclosed: the validator's duplicate-id, causation and non-state-event checks are skipped
   in that W1. W2 correctness against the oracle is unchanged. Projected 1M W1 peak is about
   7 GB (177 MB at 10K, 790 MB at 100K), which fits.

**Blocker (measured):** each shard's DuckDB `raw.sim_events` primary key on `event_id` is an
in-memory ART index. Inserting the 100K archive into one DuckDB file (memory_limit 512 MB,
threads 1) used 227 MB of DuckDB memory at 1.2M rows and 445 MB at 2.4M rows, about 180 MB per
million rows. It failed with `OutOfMemoryException` between 4.9M and 6.1M rows. A 1M W2 holds
about 97M events, about 12M per shard at 8 shards, so it would need about 17 GB of index memory
in total. That is more than the host's free memory at any shard count or cap. The run would
also take about 6 h at the measured 100K throughput (pre-registered limit 21,600 s, floor 4,500
ev/s). I did not start a run that is expected to fail on memory.

Options for the owner (none implemented; each needs an ADR):
(a) partition the local warehouse fact by event date (one table or file per day) so that
deduplication stays inside a bounded index, as BigQuery does with partitioned MERGE;
(b) drop the primary key and deduplicate at read or by a periodic compaction, which changes the
warehouse's idempotency guarantee from per-insert to eventual;
(c) run 1M on a host with about 32 GB RAM, or on BigQuery, which needs cost approval.

## Bottlenecks found (in the order profiled)

1. **Warehouse trickle** (fixed): the virtual clock jumped to the next single message, so 1-3
   rows went into each DuckDB insert. Fixed with the micro-batch window. At 3K: 828 s to 224 s.
   It returned in part at 100K: an average of 7.2 rows per warehouse round, against 74.5 at
   10K (8 shards). An instrumented 40K-event slice shows 3,844 empty pulls and 1,488 pulls of
   1-9 rows. Denser streams wake the drain at many distinct times within one second. It did not
   break an SLO at 100K. It is the first throughput item for any later 1M attempt.
2. **Telemetry counter per delivery** (fixed): counters are now aggregated before export.
3. **Postgres commit per stateful event** (kept by design): about 2-4 ms per transaction, so one
   writer is commit-bound. Concurrency is how it scales: 1 shard gave 1,031 ev/s and 8 shards
   gave 8,340-10,916 ev/s at 10K. `synchronous_commit = off` was rejected for financial state.
   W4 (27.6 enrolments/s) is bound the same way and was not parallelised in this phase.
4. **Memory**: `run-local` keeps every event; the scale harness streams. The W1 oracle id set
   (fixed by `oracle_lean` for 1M) and the DuckDB primary-key index (open, above) remain.
5. **CPU at 100K**: load1 reached 30 on 16 cores (8 shards, Postgres, sampler, desktop). More
   shards would contend for the same cores.

## Optimisations (each one change, measured before and after, oracle kept)

Re-measured on development seed 1 (`phase-14-optimisations-seed1.json`). The first pairs used
`run-local`'s default seed 42 (`phase-14-optimisations.json`), which is outside the Phase 14
seed protocol. They are kept for disclosure and point the same way.

| Change | Workload | Before | After | Oracle / checksum |
| --- | --- | --- | --- | --- |
| Counter aggregation (`StreamMetrics` -> OTel every 1,024 increments and at snapshot) | 1K x 28 d, 100,862 events, window 0, ABAB | 1,698.9 / 1,696.5 ev/s | 1,962.3 / 1,936.9 ev/s (+15%) | match; `cfde7022...` in all four |
| Warehouse micro-batch window (1 virtual s) | 3K x 28 d, 298,260 events | 772.4 s, 386.2 ev/s, 317,178 warehouse rounds | 193.5 s, 1,541.3 ev/s (4.0x), 34,304 rounds | match; `41d330a6...`, ledger 155,100,223 both |
| DuckDB threads = 1 per shard (1M only) | 10K, 8 shards | 8,340.2 ev/s | 10,915.6 ev/s | match; `8a3d03ad...` |
| Lean W1 oracle (1M only) | 100K W1 | 1,757 MB peak RSS, 248 s | 790 MB, 182 s | identical checksum `40600e56...` |

Telemetry overhead: the Phase 11 measurement put it at about 32% of chaos throughput. The
counter half is now recovered (+15%). Spans and the lag histogram are still recorded per
delivery, so the overhead is reduced, not removed.

Rejected after measurement: a multi-row `VALUES` insert (24.2 s against 13.5 s for the NDJSON
path, 199K rows); larger warehouse pull batches under chaos (a crash redelivers the whole
batch).

`benchmarks/perf_baseline.json` was **not** re-recorded. `make nightly-perf` passes against
the existing local baseline: chaos 1,938.5 ev/s, oracle match, checksum `c8928ca9...` (same as
before Phase 14). The simulator ran at 67.5K ev/s.

## Tests executed

| Suite | Result |
| --- | --- |
| `tests/scale/` (harness, oracle, sharded replay under crash rate 0.05, tamper detection, SLO and HTTP evaluators, CLI, config pin, no held-out seeds) | 18 passed |
| `tests/streaming/test_metrics_aggregation.py` (counts seen exactly once at snapshot, threshold flush, telemetry swap, collected instance keeps counts) | 5 passed |
| `tests/streaming/test_memory_broker.py` (batch window fills batches without losing messages, ignored by non-batch workers, negative window rejected) | passed |
| `tests/payments/test_load.py` (exact under duplicates; CLI) | 2 passed |
| `make check` (everything CI runs, including coverage floors and import contracts) | **exit 0**: 1,976 passed, 2 skipped, total coverage 98.83%, floors OK on 219 files |
| `make nightly-perf` | **exit 0** (above) |
| `make nightly-security` | **exit 2**, only on the known gitleaks false positive `generic-api-key` in `tests/payments/test_ids_and_model.py:44` (commit 105c9306, Phase 7: a Stripe lookup-key string). pip-audit: no known vulnerabilities; npm audit: 0; trivy on Terraform: 0 misconfigurations (run standalone) |

## Failures found / fixes

* `make audit` failed at the start (pyasn1 0.6.4, 4 CVEs): bumped to 0.6.5 in `uv.lock`.
* A collected `StreamMetrics` lost its buffered counts (found by the new test): it now flushes
  on finalisation.
* The redrive loop could spin on letters that cannot be decoded: it now stops when a page
  republishes nothing.
* W4 first imported the streaming pipeline, which reaches the simulator and breaks the
  "payments are independent of the simulator" contract. It now applies events through
  `ControlPlaneStore` directly.
* The 1M W2 cannot run on this host (DuckDB primary-key index memory). This is open; see 1M.

Disclosed contract change: `tests/scale/test_config.py` first pinned every ladder step. When a
development step was added (`10k-sharded-t1`), the pin was narrowed to the workload, the HTTP
and payment SLOs, and the customers and SLO of the gated steps only. The hash was recomputed.
No workload or SLO value changed, and this was not done after a failed result.

## Known limitations

* The transport is the in-memory broker on one host: the numbers bound the consumers, Postgres
  and DuckDB, not Pub/Sub, Cloud Run or BigQuery. No cloud run was made (GBP 0).
* Consumer lag is wall-clock time from send to handling inside a replay that publishes 5,000
  events at a time. It is reported but not gated: it measures replay queueing, not a live
  steady-state stream.
* BigQuery scanned bytes were not measured; the sandbox needs owner approval. Partitioning by
  `event_date` and clustering are analysis only (ADR 0022).
* W4 ran at 10K only, single writer. W3 ran at 100 rps total for 60 s.
* Single runs per configuration at 10K and 100K (no repeats); the 100K run took 36 min.
* The 10K 8-shard runs and the payment run were on a desktop with other load; host memory
  baselines are recorded in each report.

## Cloud cost incurred

GBP 0. Nothing was provisioned; no Stripe, BigQuery or other paid API was called.

## Gate

100K: PASS. 1M: NOT RUN (blocked, owner decision). Phase 14 gate as a whole: **NOT PASSED**.

## Reason

Every 100K metric that required_test.md s20 asks for is published above and meets the
pre-registered SLOs, with the final state equal to the oracle under chaos. The 1M milestone has
no measurement: on this host, a measured DuckDB primary-key index limit makes the run fail on
memory, and the remedy is an architectural decision for the owner.
