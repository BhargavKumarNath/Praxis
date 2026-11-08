# Load-test report

**Scope:** local, one host. 16 logical cores, 14.4 GiB RAM, 5-6 GiB of it already used by
the desktop. Postgres 17.11 runs in Docker, with DuckDB as the warehouse. The transport is an
in-memory broker with seeded chaos. Everything is **SYNTHETIC**, and there are no Stripe or
public-API calls (CLAUDE.md s5, s15). The numbers bound the consumers, Postgres and DuckDB.
They **do not** measure Pub/Sub, Cloud Run or BigQuery at scale.

Protocol (ADR 0022; the M1-budget ladder of ADR 0028 uses the same workloads in a capped budget):
- W1 simulates once into an 8-shard gzip archive and computes the oracle while streaming.
- W2 replays through 8 shard processes over ONE Postgres, with these faults: 10 % duplicates,
  delays up to 600 s, reordering, and consumer crashes at a rate of 0.005.
- W3 is k6 HTTP load.
- W4 drives the `SyntheticPaymentGateway`, with each webhook delivered twice.

Workloads and SLOs are hash-pinned in `configs/scale/slo.toml`. Seeds: simulator 1, fault 701.

## 1. Ladder

| Step | Events | Wall | Throughput | Op apply p99 | Peak memory | Correct (oracle) | Source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1K x 28 d (Phase 3, single process, seed 42 sim world) | 99,979 | 56.7 s | 1,762.7 ev/s | - | RSS 532 MB | match | [run-local-1k](../evidence/phase-3-run-local-1k.json) |
| 10K x 28 d (Phase 3, single process) | 977,754 | 1,887.5 s | 518.0 ev/s | - | RSS 1,801 MB | match | [run-local-10k](../evidence/phase-3-run-local-10k.json) |
| 10K, 1 shard | 974,034 | 944.9 s | 1,030.8 ev/s | 5.8 ms | host 6,560 MB | match | [10k-1shard](../evidence/phase-14-10k-1shard-w2.json) |
| 10K, 8 shards, DuckDB threads 1 | 974,034 | 89.2 s | 10,915.6 ev/s | 7.3 ms | host 7,775 MB | match | [10k-threads1](../evidence/phase-14-10k-8shards-threads1-w2.json) |
| **100K, 8 shards (gated)** | **9,737,517** | **2,166 s** | **4,494.6 ev/s** | **21.3 ms** | **host 10,959 MB** | **match, 0 deadlocks** | [100k W2](../evidence/phase-14-100k-w2.json) |
| m1-100k: 4 GB no-swap scope, 8 CPUs, 6 shards; Postgres 1 GB (ADR 0028) | 9,737,517 | 1,894 s | 5,141.2 ev/s | 13.3 ms | scope 3,794 MB, 0 OOM | match, 0 dup ids | [final-scale.md](../evidence/final-scale.md) |
| m1-250k (same budget) | 24,343,371 | 4,731 s | 5,145.9 ev/s | 12.2 ms | scope at its 4,096 MB cap (page cache), 0 OOM | match, 0 dup ids | same |
| m1-500k (same budget; Postgres swap uncapped, 14.5 MB) | 48,732,359 | 11,150 s | 4,370.4 ev/s | 16.5 ms | scope at its 4,096 MB cap (page cache), 0 OOM | match, 0 dup ids | same |
| 1M (Phase 14 and m1-1m) | - | - | - | - | - | **NOT RUN** (Phase 14: index memory; m1-1m: owner decision) | [phase-14.md](../evidence/phase-14.md) |

More 100K detail:
- Operational apply latency: p50 4.4 ms, p95 13.9 ms, max 349 ms.
- Warehouse insert latency: p99 2.98 ms.
- CPU: 9.0 of 16 cores used on average.
- Postgres: 10 connections; 1,025,044 commits and 458 rollbacks (conflict retries); cache hit
  98.2 %.
- Chaos absorbed: 4,700 operational and 92,261 warehouse crashes, plus 210 dead letters,
  all redriven to success.
- Consumer lag p99 was 25.4 s. It is reported, not gated: it measures replay queueing, not a
  steady stream.

## 2. HTTP (W3, k6 pinned by digest, against the 100K database)

50 rps per surface for 60 s, 6,001 requests in total, 0 failed and 0 dropped:

| Surface | p95 | p99 |
| --- | --- | --- |
| API | 80.6 ms | 184.1 ms |
| Console | 99.5 ms | 154.4 ms |

Source: [phase-14-100k-http.json](../evidence/phase-14-100k-http.json).

## 3. Payments (W4, 10K, synthetic gateway only)

- 10,000 enrolments, 981 of them with failing cards.
- 40,000 notifications, each delivered twice, produced **0 duplicate inbox rows**.
- The ledger has 9,019 entries totalling 44,193,100 minor units, equal to the gateway's paid
  invoices.
- Enrolment p99 33.8 ms. Throughput 27.6 enrolments/s (single writer, commit-bound).

Source: [phase-14-payments-10k.json](../evidence/phase-14-payments-10k.json).

## 4. Regression tracking (`make nightly-perf`)

The baseline is in `benchmarks/perf_baseline.json` (`local`). The check fails if throughput
drops below 0.5x the baseline, if peak RSS exceeds 1.5x, or if a chaos run diverges from its
oracle. At the end of Phase 15 the chaos throughput was 1,764 ev/s (floor 1,218). The Phase 16 run
(2026-11-05) measured 1,800.9 ev/s chaos and 72,555 ev/s for the simulator, both inside the
bands ([phase-16.md](../evidence/phase-16.md)).

**Final engineering run (2026-11-08).**
- The first `nightly-perf` after ADR 0028 **FAILED**: 575 ev/s against the 1,218 floor.
- Cause: streaming writers compacted after every micro-batch, which is quadratic.
- Fix: ADR 0028 amendment 1, which compacts every 50,000 rows and flushes before reads.
- The re-run passed at **1,541.9 ev/s**, about 14 % below Phase 16. The baseline was not
  re-recorded.

## 5. Bottlenecks and what is not claimed

- **The Phase 14 1M blocker was removed.** The DuckDB `event_id` primary-key index (about
  180 MB per million rows, OOM at 4.9-6.1M rows per shard) was replaced by a landing table
  plus date-scoped anti-join compaction (ADR 0028).
- The M1-budget ladder then replayed 100K, 250K and 500K correctly inside a 4 GB scope. Details
  and deviations are in [final-scale.md](../evidence/final-scale.md). **1M was not run** (owner
  decision), and nothing above 500K is claimed.
- At 500K, throughput fell 15 % as the Postgres database (2.5 GB) outgrew its 1 GB container:
  the cache hit ratio dropped from 98.5 % to 94.6 %. Postgres memory is the next bottleneck in
  this budget.
- The Phase 14 100K figures in the first table predate ADR 0025 amendment 1 and ADR 0028. The
  m1 rows are the final code.
- No cloud load test was run. Pub/Sub, Cloud Run and BigQuery behaviour at scale is
  **unmeasured**.
- **No claim of support for millions of customers is made.**
