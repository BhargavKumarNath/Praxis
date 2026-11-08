# Final scale evidence: the M1-budget ladder (ADR 0028)

**All data is SYNTHETIC** (simulator seed 1, fault seed 701, 28 days). These are **local
measurements on one Linux host that EMULATES the memory budget of an 8 GB Apple M1**. They are
not M1 measurements, not cloud measurements and not real traffic.

## 1. Question and registration

The question: what is the largest step that replays **correctly** inside the budget of the
owner's development machine (Apple M1, 8 GB unified memory)?

The steps, their checks and the budget were registered in `configs/scale/slo.toml` on
2026-11-05, before any of these steps ran. The file is hash-pinned by
`tests/scale/test_config.py`, and the Phase 14 registration stays pinned and unchanged.

**Budget, as emulated** (`make scale-m1 SCALE=m1-<n>`):

| Part | Limit |
| --- | --- |
| Harness (W1 simulate + archive + oracle, then W2 with 6 shard processes) | One cgroup v2 scope: `MemoryMax=4096M`, `MemorySwapMax=0`, pinned to CPUs 0-7 (`taskset -c 0-7`). Going over the memory limit is an OOM kill |
| Postgres | Its own container `praxis-pg-m1`: `--memory 1g --memory-swap 1g --cpus 2` |
| Left for macOS and a desktop | About 3 GB of the 8 GB |

**Checks.** Every check must pass:
- final control-plane state equals the oracle;
- ledger equals the oracle;
- 0 subscription inconsistencies;
- 0 Postgres deadlocks;
- error rate 0;
- warehouse rows equal archived events, with 0 duplicate event ids;
- 0 OOM kills in the scope;
- 0 swap in the scope;
- wall time at most 6 h;
- operational apply p99 at most 50 ms.

Throughput is reported, not gated. The scope's `memory.peak` counts page cache and cannot
exceed `memory.max`, so it is reported, and the OOM-kill count is the binding check.

## 2. Results

| Step | Events | W2 wall | Throughput | Op apply p99 | Shard RSS max | Scope OOM / swap | Oracle / ledger / warehouse | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| m1-10k (calibration, no SLO) | 974,034 | - | 4,757 ev/s | - | - | 0 / 0 | match | calibration |
| **m1-100k** | **9,737,517** | 1,894 s | 5,141.2 ev/s | 13.3 ms | 631 MB | 0 / 0 MB | match / match / equal, 0 dup | **PASS 12/12** |
| **m1-250k** | **24,343,371** | 4,731 s | 5,145.9 ev/s | 12.2 ms | 648 MB | 0 / 0 MB | match / match / equal, 0 dup | **PASS 12/12** |
| **m1-500k** | **48,732,359** | 11,150 s (3.1 h) | 4,370.4 ev/s | 16.5 ms | 662 MB | 0 / 0 MB | match / match / equal, 0 dup | **PASS 12/12, with the Postgres deviation in s4** |
| m1-1m | - | - | - | - | - | - | - | **NOT RUN** (owner decision 2026-11-06). Neither a pass nor a fail |

Sources:
- m1-10k: [W1](final-scale-m1-10k-w1.json), [W2](final-scale-m1-10k-w2.json).
- m1-100k: [W1](final-scale-m1-100k-w1.json), [W2](final-scale-m1-100k-w2.json), [Postgres](final-scale-m1-100k-pg-memory.json).
- m1-250k: [W1](final-scale-m1-250k-w1.json), [W2](final-scale-m1-250k-w2.json), [Postgres](final-scale-m1-250k-pg-memory.json).
- m1-500k: [W1](final-scale-m1-500k-w1.json), [W2](final-scale-m1-500k-w2.json), [Postgres](final-scale-m1-500k-pg-memory.json), and the [interrupted run's W1](final-scale-m1-500k-w1-interrupted-old-code.json).

Every step's oracle checksum equals the observed checksum:
- m1-100k: `40600e56…`
- m1-250k: `b55cbf81…`
- m1-500k: `a98ab194…`

The m1-100k control-plane checksum (`638fca67…`) is **identical to the Phase 14 100K run's**.
That run used 8 shards on an unconstrained 14.4 GiB host. Two different budgets and shard
counts therefore produced the same final state.

**Chaos absorbed at 500K.**
- 23,211 operational consumer crashes and 465,210 warehouse consumer crashes.
- 256 dead letters, all redriven, with 0 open afterwards.
- Postgres: 5,122,293 commits and 2,249 rollbacks (conflict retries); 0 deadlocks; peak 8
  connections; 0 lock waits.

**Latency at 500K.**

| Consumer | p50 | p95 | p99 | max |
| --- | --- | --- | --- | --- |
| Operational apply | 3.9 ms | 13.1 ms | 16.5 ms | 1,192 ms |
| Warehouse | 0.42 ms | 1.85 ms | 4.29 ms | - |

## 3. Measured resource use

| Step | W1 peak RSS | Scope memory.peak | Host used peak | Postgres container peak / OOM | Postgres DB | Archive (gzip) | Warehouse (6 DuckDB files) | Postgres cache hit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| m1-100k | 559 MB (old W1 code) | 3,794 MB | 8,101 MB | 1,024 MB (= cap) / 0 | 516 MB | 871 MB | 1,100 MB | 98.5 % |
| m1-250k | 1,148 MB (old W1 code) | 4,096 MB (= cap, page cache) | 7,868 MB | 1,024 MB / 0 | 1,271 MB | 2,177 MB | 2,542 MB | 96.4 % |
| m1-500k | 1,038 MB (new W1 code) | 4,096 MB (= cap, page cache) | 8,652 MB | 1,024 MB / 0; **swap peak 14.5 MB** | 2,534 MB | 4,357 MB | 4,752 MB | 94.6 % |

- Storage per 100K customer-months is about **2.4 GB** in total: 0.87 GB archive, 1.0 GB
  warehouse and 0.5 GB Postgres. It grows linearly over the three steps.
- The 500K step uses about **11.6 GB of disk**. A 256 GB M1 has room for it. The figure is
  stated so the owner can check before a real run.
- **W1 memory fix.** This was a disclosed change, made after m1-250k with no criterion
  changed; see `slo.toml` and ADR 0028.
  - Before the fix, W1 grew by about 4.2 KB per customer, and the interrupted 500K run's W1
    peaked at 2,128 MB.
  - The re-run with the new code peaked at **1,038 MB**.
  - Both runs gave the **same archive (4,356,628,647 bytes) and oracle checksum (`a98ab194…`)**.
  - W1 at 100K fell from 559 to 307 MB on a development measurement.
- **Throughput fell 15 % from 250K to 500K** (5,146 to 4,370 ev/s). The measured fact is that
  the Postgres database (2.5 GB) outgrew its 1 GB container and the cache hit ratio fell from
  98.5 % to 94.6 % (6.8M blocks read, against 2.2M at 250K). The concurrent load in s4 may also
  have contributed. The split between the two was not measured.

## 4. Deviations and concurrent work (disclosed)

1. **Postgres swap was not capped during the 500K run.** The host rebooted between the
   interrupted run (2026-11-06) and the re-run (2026-11-08), and `docker start praxis-pg-m1`
   restored `memory.max = 1 GB` but left `memory.swap.max = max`. The container config still
   says `--memory-swap 1g`. During the 100K and 250K runs `memory.swap.max` was 0.
   - **Effect:** Postgres swapped up to **14.5 MB** (`memory.swap.peak`), exceeding its
     registered 1 GB RAM / 0 swap budget by about 1.4 %.
   - **What it did not affect:** the gated "no swap" check covers the harness scope, which
     had swap 0 and passed. Every correctness check passed.
   - **Verdict:** m1-500k is therefore reported as **PASS 12/12 with this environment
     deviation**. **m1-250k is the largest step measured fully inside the registered budget.**
   - **Fix, made after the run:** `make scale-m1-pg` now re-applies
     `docker update --memory 1g --memory-swap 1g` and refuses to run unless the container's
     cgroup reads 1 GB / 0 swap.
   - **The step was not re-run:** that would take about 3.3 h. Re-running it on the fixed
     target is an open item.
2. **Concurrent work on the host** (all of it outside the harness scope's CPUs 0-7):
   - **250K:** development work ran on CPUs 10-15.
   - **500K re-run:** `make check` ran 3 times, plus `nightly-security`, `e2e`, `agent-bench`,
     `agent-llm-protocol`, `resilience`, `nightly-science` and `stripe-verify`. All were
     started under `taskset -c 10-15`.
   - **Containers:** the test Postgres and Pub/Sub emulator containers were pinned to CPUs
     10-15 with `docker update --cpuset-cpus` about 30 minutes into the run. Before that, and
     for tool containers started by those targets (Terraform, gitleaks, trivy, actionlint),
     Docker did not inherit the CPU pinning.
   - **`praxis-pg-m1`** had a 2-CPU quota but no cpuset, as in every step.
   - Host memory used peaked at 8,652 MB of 14.4 GiB, so no step was short of host memory.
3. **The 500K W1 ran on the new W1 code.** The 100K and 250K W1 runs used the old code. W2 is
   unaffected, because it reads only the archive, which is byte-identical.
4. Each step ran **once**. No repeats, no variance estimate.

## 5. Comparison with Phase 14

| | Phase 14 100K (ADR 0022) | m1-100k (ADR 0028) |
| --- | --- | --- |
| Budget | Whole host: 16 threads, 14.4 GiB, Postgres uncapped | 4 GB no-swap scope on 8 CPUs; Postgres 1 GB / 2 CPUs |
| Shards | 8 | 6 |
| Warehouse | DuckDB with an `event_id` primary-key index (needs about 180 MB per million rows; OOM at 4.9-6.1M rows per shard) | No index: landing table plus date-scoped anti-join compaction every 50,000 rows |
| Throughput | 4,494.6 ev/s | **5,141.2 ev/s** |
| Op apply p99 | 21.3 ms | **13.3 ms** |
| CPU cores used (average) | 9.0 | **3.7** |
| Host memory peak | 10,959 MB | **8,101 MB** |
| Shard RSS max / sum | 740 / 5,733 MB | **631 / 3,738 MB** |
| W1 peak RSS | 1,757 MB (lean oracle 790 MB) | 559 MB (307 MB after the W1 fix) |
| Final state | `638fca67…` = oracle | `638fca67…` = oracle (identical) |
| Largest correct step | 100K (1M blocked by the index) | **500K** (with the s4 deviation; 250K fully in budget). 1M not run by owner decision |

## 6. What this emulation does NOT show

- **Not an M1.** CPU speed and microarchitecture, the macOS memory compressor, unified memory,
  APFS and the Apple SSD all differ, and Docker on macOS runs Postgres inside a Linux VM whose
  memory also counts. The cgroup caps reproduce the memory **budget**, not M1 performance. A
  real M1 run is the owner's to make; the same target is not portable, because `systemd-run`
  is Linux-only.
- **Not the cloud.** The transport is the in-memory broker with seeded chaos. Pub/Sub, Cloud
  Run and BigQuery at this scale are **unmeasured**.
- **Not live traffic.** W2 is a full replay of 28 days. 500K customers is about 20 events/s
  on average live; the replay pushes 4,370 ev/s. Consumer lag here measures replay queueing,
  not live latency.
- **Not 1M.** It was not run, by the owner's decision. Nothing here is a projection to 1M, and
  **no claim of support for millions of customers is made.**

## 7. Reproduce

```bash
make pg-up
make scale-m1 SCALE=m1-100k     # ~35 min; m1-250k ~1.5 h; m1-500k ~3.3 h, ~12 GB disk
```

Reports go to `data/scale/<step>/{w1,w2,pg-memory}.json`; copies are in this directory.
