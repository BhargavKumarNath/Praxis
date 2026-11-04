# Phase 15 performance optimisation evidence (2026-11-01)

All data is SYNTHETIC. No Terraform apply / destroy, deployment, BigQuery query or Stripe
call was made; the only cloud call was one read-only Service Usage quota listing. Cloud cost
incurred: GBP 0. Earlier evidence is kept unchanged:
* `phase-15.md` and `phase-15-failures.json` (fault seed 801, 10 / 12);
* `phase-15-remediation.md`;
* `phase-15-remediation-802-failures.json` (802, 12 / 12, flow control v1);
* `phase-15-remediation-803-failures.json` (803, 12 / 12; renamed from
  `phase-15-remediation-failures.json`, byte-identical, sha256 `0c368458…`).

## Pre-registered acceptance (written before any final run)

The final code must meet all of these, with no threshold, baseline or benchmark parameter
changed:

1. `make nightly-perf` exits 0 against the unchanged `local` baseline: chaos throughput
   ≥ 1,218 ev/s (0.5 × 2,436.5), peak RSS ≤ 1.5 × baseline, and the chaos run matches its
   oracle.
2. The optimisation changes no delivery behaviour. On the nightly-perf world, crashes,
   deliveries and rounds per subscription are identical to the remediated code
   (warehouse 972 / 164,758 / 16,899; operational 43 / 5,115 / 10,294), with 0 dead
   letters.
3. Resilience: `configs/resilience/remediation-804.toml` (identical criteria; fresh fault
   seed 804, pinned before the run) passes 12 / 12, run once.
4. `make -k check` and `make nightly-security` exit 0.

If any of these fails, it is reported as a failure.

## Results

| Pre-registered item | Result |
| --- | --- |
| 1. `make nightly-perf` | **PASS (exit 0)**: chaos 1,764.3 ev/s ≥ 1,218; peak RSS 536.8 MB ≤ 797.9; oracle match; simulator 74,140 ev/s |
| 2. Delivery behaviour unchanged | **PASS**: warehouse 972 / 164,758 / 16,899 and operational 43 / 5,115 / 10,294, identical in all 3 A/B runs and in nightly-perf; 0 dead letters |
| 3. Resilience on fault seed 804 (one run) | **PASS: 12 / 12** (`phase-15-remediation-failures.json`, criteria sha256 `607dec4a…`) |
| 4. `make -k check` / `make nightly-security` | **PASS**: exit 0 (2,035 passed, 2 opt-in live skips, coverage 98.85 %, floors OK) / exit 0 |
| Also: `make e2e` | PASS (1 journey) |

### Reproducible measurement

`run-local --customers 1000 --days 28` (the nightly-perf configuration), on a freshly created
and migrated Postgres database per run. The delivery trace is deterministic: identical counts
in every run. Throughput varies with machine load, about ±5 % here. During these runs a
desktop file indexer used about 80 % of one core and the CPU governor was `powersave`. The
earlier 932 vs 954 ev/s difference was this noise, since the work done was identical. The
runs were interleaved so that drift hits every variant equally.

| Code (fresh DB, 3 interleaved runs) | Chaos ev/s, median (range) | Warehouse writes | Warehouse deliveries |
| --- | --- | --- | --- |
| HEAD 2272c3a (pre-Phase 15) | 2,059 (2,019–2,147) | 2,450 | 205,889 |
| Remediated (ADR 0025, ON CONFLICT writes) | 980 (939–987) | 16,899 | 164,758 |
| **Optimised** (ADR 0025 + amendment 1) | **1,718 (1,711–1,800)** | 16,899 | 164,758 |

### Profiling

Remediated code under cProfile, 145 s total:
* `insert_events`: 66 s self time over 17,118 calls;
* 11,263 of those were single-message writes of messages delivered three or more times;
* about 6,100 were slow-start ramp writes after the 972 crash restarts.

Per call, one row cost about 3.2 ms:
* the temporary file: 0.15 ms;
* the `read_json` scan: 0.85 ms;
* `ON CONFLICT` into the primary-key table: about 2.2 ms (a plain INSERT is about 0.7 ms).

Optimised code: 90 s total, with `insert_events` taking 29 s cumulative. 9,596 calls are now
a lookup only (redeliveries of rows already stored). The rest of the time is spread across
Postgres, pydantic and JSON, with no single hotspot.

Micro-benchmarks on a table pre-filled with 50k rows:

| Rows | Old path | New path |
| --- | --- | --- |
| 1 | 3.06 ms | 0.81 ms |
| 100 | 5.86 ms | 4.12 ms |

The primary-key `IN` lookup stays flat from 50k to 3M rows (0.2 ms for 1 id, 1.2 ms for 100).

### The earlier unexplained oracle mismatch: explained, not a defect

It was reproduced on demand:
* A 300-customer world run on a database already holding the 1,000-customer world always
  mismatches. The snapshot holds the other world's rows. The transition guard
  `guard_state_move` also correctly rejects contradictory moves (`paid -> open`,
  `lost -> active`; 1,659 `IntegrityError` nacks), so 153 messages are dead-lettered.
* Re-running the same world on the same database matches, with an identical checksum
  (`c8928ca97336`) and 0 dead letters, so idempotency across restarts holds.
* Every test uses its own template-cloned database. `make nightly-perf` / `events-check` drop
  and recreate theirs.

The exposure was manual `run-local` on a reused database. `run-local` now refuses a control
plane that already holds processed events (`ControlPlaneStore.has_processed_events`, tested
in `tests/streaming/test_cli.py`).

### BigQuery quota unit: verified

See `docs/cost-guard.md`:
* the read-only `gcloud alpha services quota list` reports metric unit `MiBy` and limit
  `1/d/{project}`, default 209,715,200;
* Google's unit grammar defines `Mi` = 2^20 and `By` = byte;
* BigQuery's quotas page gives a 200 TiB/day default, and 200 × 2^20 = 209,715,200.

The module's `bigquery_daily_query_mib = 10240` is therefore in the right unit.

## Limitations

* Chaos throughput is about 17 % below HEAD: isolation still needs about 7× more warehouse
  writes. It is above the floor with a margin of about 40 % (1,718 median vs 1,218).
* Measured on one noisy developer machine. The `github` baseline is still unrecorded.
* All resilience limitations of `phase-15-remediation.md` remain:
  * in-process crashes, not OS kills;
  * the emulator, not managed Pub/Sub;
  * no hard bound below 5 attempts; 0 healthy dead letters is measured on seeds 803 and 804,
    not proven.
* The quota unit was read on an existing project, because the Praxis project does not exist
  yet. The quota is approximate, not a hard cap.
