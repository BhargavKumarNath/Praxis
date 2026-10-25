# Evidence: Phase 11 wiring completed (pre-Phase 13 readiness)

```text
Scope: Phase 11 carry-over only (feeds, cross-process metrics, scheduler, overhead measurement).
       Not a new phase gate; Phase 11 and 12 gates are not reopened.
Date: 2026-10-25
Code revision: ce45230 + uncommitted working tree (nothing committed or pushed)
Environment: local (Linux, Python 3.12, Postgres 16 `praxis-pg`, Pub/Sub emulator); GBP 0
Design: ADR 0020
```

## Audit before the change (what was not wired)

| Item | Finding |
| --- | --- |
| `OpsMonitor.tick` | no caller in `src` (tests and drills only): alerts never became incidents on their own |
| `data_source_age_s`, `data_contract_failures_total` | never written anywhere |
| `business_kpi_deviation` | `signals.record_kpi` existed, no caller |
| `queue_backlog`, `dlq_backlog` | `signals.sample_backlog` existed, no caller |
| Metrics scope | in-process only: the API (future dashboard) saw its own HTTP metrics, never the consumers', pricing's or MLOps' |

## What was built

- `praxis.control.metric_snapshots` + migration 0008: per-process snapshots with expiry,
  merge rules (counters / histograms per instance, newest gauge wins), stale reporting,
  catalogue check on read, a publisher that never raises and backs off on failure.
- `praxis.ops.collect`: warehouse (source age, contract failures over 24 h), dead letters
  (per subscription, 0 once drained), revenue KPI (SYNTHETIC ledger, last complete day vs
  median of up to 28 days, at least 7).
- `praxis.ops.system.SystemReader` / `build_reader`: one fleet view plus its inputs for the
  monitor, `GET /v1/ops/health`, `/ops/dashboard` and the agent tools.
- `python -m praxis.ops run`: the scheduler (tick, persist incidents, publish, prune,
  survives a database outage).
- Publishers: API (lifespan, 15 s), `streaming consume` (pull-loop heartbeat + exit),
  `streaming run-local` (once, with sampled `queue_backlog`), `pricing decide` (with a
  database), `mlops run-day` / `operate`.
- Health report `inputs` (JSON and HTML: STALE processes, collector status).
- Agent read tools: the fixed "no automatic feed" / "no scheduler" / "in-process" labels were
  replaced by notes computed from the actual inputs (scope, stale processes, collector status,
  whether the ops monitor is running). `configs/agent/*` (pinned) unchanged.
- Small additions: `Warehouse(read_only=True)`, `Warehouse.quarantined_batches`,
  `ControlPlaneStore.dead_letter_backlog` / `daily_paid_revenue`, `run_pull_loop(heartbeat=)`,
  `streaming.metrics.record_backlog`.

## Tests executed

- `tests/control/test_metric_snapshots.py` (11): merge rules, expiry boundary, upsert, local
  vs own copy, hostile / unknown points dropped and counted, DB constraints, prune, publisher
  throttle and outage back-off.
- `tests/ops/test_collect.py` (9, real DuckDB file + Postgres): source age and windowed
  contract failures through the PINNED rules (a source that stops delivering now fires
  `source_stale`), missing warehouse marks only its collector and is never created, collector
  bugs are not swallowed, DLQ backlog incl. drained 0, revenue KPI (deviation -0.5 fires
  `business_kpi_shock`), insufficient history and empty ledger, fleet merge with a stale
  process, store-down degrades to process scope.
- `tests/ops/test_monitor_run.py` (7): another process's metrics open a persisted incident
  through the real `ops run` loop, dedup on the second tick, resolve after two clear ticks,
  no-database exit, failed tick survives, health API fleet inputs + STALE in HTML + the API's
  own published row, process scope without a database.
- Extended: `tests/streaming/test_cli.py` (run-local publishes the sampled backlog, no DLQ
  label), `tests/streaming/test_pubsub_unit.py` (heartbeat per pull),
  `tests/pricing/test_cli.py` (cycle publishes, 26 h TTL), `tests/mlops/test_cli.py` (mlops
  row after `operate`), `tests/agent/test_tools.py` and `test_cli.py` (fleet scope, stale
  process named, monitor running), `tests/control/test_migrations.py` (head 0008).
- `make nightly-perf` (consumer path touched: heartbeat + end-of-run publish).
- Telemetry overhead measurement (Phase 11 carry-over), protocol pre-registered in
  `arm.py` before any run: event-chaos run with the nightly-perf configuration (1,000
  customers, 28 days), order real, stubbed, real, stubbed; median per arm.
- `make check`: first run failed the coverage gate (see Failures found); rerun after the fix:
  **exit 0**. pytest 1,888 passed, 2 skipped (opt-in live BigQuery and Stripe suites); total
  coverage 98.80%, floors OK on 203 files; 6 import contracts kept; ops-check, events-check,
  schemas, secret scan, pip-audit and npm audit (0 vulnerabilities), frontend, terraform
  validate, actionlint and zizmor clean. `nightly-security` not run (no dependency or secrets
  handling changed).

## Results

- `make nightly-perf`: exit 0. Simulator 71,327 ev/s, 108.8 MB; event chaos 1,635 ev/s
  (0.67x the local baseline 2,436.5; Phase 11 measured 0.69x), 592.4 MB (1.11x). Within the
  gate. Baseline NOT re-recorded.
- Telemetry overhead (measurement, not a gate):

  | Run | Arm | events/s | matches oracle | events |
  | --- | --- | --- | --- | --- |
  | 1 | real | 2,172.9 | yes | 99,979 |
  | 2 | stubbed | 3,192.6 | yes | 99,979 |
  | 3 | real | 2,100.2 | yes | 99,979 |
  | 4 | stubbed | 3,048.2 | yes | 99,979 |

  Median real 2,136.6 vs stubbed 3,120.4: **telemetry costs about 32% of event-chaos
  throughput.** That accounts for most of the Phase 11 0.69x finding. Run-to-run spread is
  large (the nightly-perf real run above gave 1,635 ev/s), so treat 32% as approximate.
  Not fixed here (it is a hot-path optimisation); recorded for Phase 14.

## Failures found

1. First `ops run` test: structured log lines share stdout with the tick lines (project
   convention), so the test parsed three lines instead of two. The test now selects tick
   lines; the CLI output is unchanged.
2. First `make check`: 1,887 passed, 2 skipped, but the coverage gate failed:
   `src/praxis/api/app.py` 92.8% < 95% (the database path of `build_system_view` /
   `build_incident_store` and the lifespan without a publisher were not exercised). Fixed by
   adding tests for those paths (`tests/ops/test_monitor_run.py`), floor unchanged, then
   `make check` was rerun in full.
3. Design corrections before any test: a single global staleness age would have marked batch
   jobs (daily) stale within a minute, so each row carries its own expiry; a failed publish
   retried on every loop iteration would have put the 5 s connect timeout into the consumer
   loop during an outage, so it backs off one interval (tested).

## Known limitations

1. Snapshots are latest values, not a time series; SLO windows are still process lifetimes.
2. `queue_backlog` is reported only by the in-memory harness; the emulator has no backlog API,
   and Cloud Pub/Sub would need Cloud Monitoring (spend: owner approval).
3. The warehouse collector reports `unavailable` while an ingest holds the DuckDB writer lock.
4. The revenue KPI uses simulated ledger days (SYNTHETIC); a finished simulation does not move.
5. The ops monitor is a local long-running process; nothing provisions it on cloud.
6. Telemetry overhead (about 32%) is measured, not reduced.
7. Unchanged: gitleaks false positive (`tests/payments/test_ids_and_model.py:44`, owner);
   Phase 4 loader defect (pricing shadow results not validated for execution); only the
   `pubsub-backlog` and `agent-failure` runbooks are exercised in tests.

## Readiness for Phase 13

Ready on the observability side: the dashboard can read real fleet state through
`/v1/ops/health` with staleness and input status per source, and incidents are produced by a
real scheduler. Still Phase 13 work (not wiring gaps): read APIs for the executive, pricing,
payments, MLOps and AI-operator views (the data is in Postgres / DuckDB), a cost-estimate
source (no cost metric exists), and the role / admin boundary for impactful controls.

## Cloud cost incurred

GBP 0.
