# Phase 14 checkpoint (Scale Engineering)

Updated: 2026-10-31. Nothing committed (owner commits). Full results: `docs/evidence/phase-14.md`.

## State
- 100K: PASS, 10 / 10 pre-registered SLOs (4,494.6 ev/s, 2,166 s, op p99 21.3 ms, host peak
  10,959 MB, oracle and ledger match, 0 open dead letters, 0 deadlocks). W3 HTTP PASS, W4
  payments (10K) PASS.
- 1M: NOT RUN. Measured blocker: the DuckDB `event_id` primary-key index (~180 MB per
  million rows; OOM at 4.9-6.1M rows per shard with a 512 MB cap); 1M needs ~17 GB of it.
  Owner decision on the warehouse design (ADR 0022 amendment lists the options).
- Changes prepared for 1M (measured on development steps): `duckdb_threads = 1`,
  `oracle_lean = true`.
- `make check` exit 0; `make nightly-perf` exit 0 (baseline not re-recorded);
  `make nightly-security` exit 2 only on the known gitleaks false positive.

## If resuming
- Decide the warehouse deduplication design (ADR), implement it, measure it at 10K and 100K
  (development seed 1, fault seed 701), then run 1M (~6 h at 100K throughput; tell the owner).
- Fix the 100K warehouse micro-batch shrinkage (7.2 rows per round) first; it is the main
  throughput risk against the 1M floor (4,500 ev/s).
- Large data lives in `data/scale/` (100K archive 847 MB, warehouses 2.8 GB): safe to delete.
