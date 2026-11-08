# SLO report

Praxis has three families of service-level objectives. Each was **pre-registered and
hash-pinned** before the run it judges, and none was loosened after a result. The single
change, `forecast_freshness` going from 48 h to 24 h, was a tightening that the owner
approved after the Phase 15 failure. It is disclosed below.

All measurements are on SYNTHETIC worlds or the synthetic live deployment.

## 1. Operational SLOs (`configs/ops/slos.toml`, ADR 0018, version 2026-11-01.1)

| SLO | Kind | Target | Where it was exercised | Result |
| --- | --- | --- | --- | --- |
| `api_availability` | ratio of non-5xx | >= 0.995 | Resilience row 3: a DB lock makes the webhook route answer 503 | Breach detected on tick 1, cleared in 2 ticks ([804 report](../evidence/phase-15-remediation-failures.json)) |
| `webhook_processing_success` | ratio of durably accepted deliveries | >= 0.99 | Resilience row 3 | Breach detected on tick 1; 0 deliveries acknowledged without a durable write |
| `pricing_decision_latency_p95` | latency | <= 2,500 ms | Every pricing cycle; one optimiser call averages 6.6 ms ([phase-6.md](../evidence/phase-6.md)) | Within target in all recorded runs (no breach recorded) |
| `event_consumer_lag_p95` | latency | <= 5,000 ms | Consumers; resilience row 12 (burst) | The burst raised `queue_backlog_high` on tick 1 and drained 52,745 events in 18.6 s |
| `forecast_freshness` | gauge max | <= 24 h (**was 48 h**) | Resilience row 8 | v1 seed 801 **FAIL** (no alert at a 2-day lag). After the amendment, seed 804 detects on tick 1 |
| `dlq_backlog` | gauge max | <= 10 | Resilience row 5 (poison storm), row 6 (restart) | Detected on tick 1; the runbook redrive clears it in 2 ticks |

Alerting uses the **two-window rule**: an alert fires only if the condition holds both
cumulatively and since the last tick. Without it, a cumulative ratio SLO could never clear
(ADR 0023). There are 16 alert rules, each with a runbook in [`docs/runbooks/`](../runbooks/).
`make ops-check` validates the set.

### Resilience suite against the SLOs (fault seed 804, final code, run once)

**12 / 12 PASS**: [`phase-15-remediation-failures.json`](../evidence/phase-15-remediation-failures.json).
The history is kept:
- seed 801: **10 / 12 FAIL** ([phase-15.md](../evidence/phase-15.md));
- seed 802: 12 / 12, on a flow-control version later replaced for throughput;
- seed 803: 12 / 12.

Phase 16 re-ran the 804 criteria once, writing to a separate file: **12 / 12 PASS**, with
every measured outcome equal to the recorded run
([phase-16-resilience-seed804-rerun.json](../evidence/phase-16-resilience-seed804-rerun.json)).

## 2. Scale SLOs (`configs/scale/slo.toml`, ADR 0022): 100K step

| SLO | Target | Measured | |
| --- | --- | --- | --- |
| throughput | >= 2,700 ev/s | 4,494.6 ev/s | PASS |
| wall time | <= 3,600 s | 2,166 s | PASS |
| operational apply p99 | <= 50 ms | 21.3 ms | PASS |
| shard RSS | <= 1,500 MB | 740 MB | PASS |
| host memory | <= 13,000 MB | 10,959 MB | PASS |
| error rate (open dead letters) | 0 | 0 of 9,737,517 | PASS |
| state = oracle / ledger = oracle | exact | exact (ledger 4,902,803,039 minor units) | PASS |
| subscription inconsistencies / deadlocks | 0 / 0 | 0 / 0 | PASS |
| HTTP p95 / p99 / failed | <= 250 ms / <= 1,000 ms / <= 0.1 % | API 80.6 / 184.1 ms, console 99.5 / 154.4 ms, 0 failed | PASS |

Source: [phase-14.md](../evidence/phase-14.md). **1M has no measurement** (NOT RUN).

## 3. Cloud and live operation (operating environment, not a gate)

| Objective | Measured | Source |
| --- | --- | --- |
| Deployment smoke | 46 / 46 on run 4. Runs 1-3 failed and are kept | [deployment.md](../evidence/deployment.md) |
| Live smoke | 49 / 49 on run 3. Runs 1-2 failed and are kept | [live-loop.md](../evidence/live-loop.md) |
| Live tick success | 11 cloud catch-up ticks: 10 operated, **1 failed** (tick 7, the 1,500 s step timeout); the next tick recovered with nothing lost. A same-day re-run was a no-op (`up_to_date`) | [live-loop.md](../evidence/live-loop.md) |
| First scheduled daily tick (2026-11-05 03:10 UTC) | `operated`, behind_days 0, steps simulate 23.4 s, load 8.7 s, dbt 17.6 s, lifecycle 13.5 s, review 2.5 s. Drift alarm (residual, segment); shadow `demand-hybrid-84d53ff98b99` still `ready_for_promotion` | `gs://praxis-dev-live-praxis-dev-510522/reports/2026-11-05.json` (read-only, Phase 16) |
| Forecast freshness against a real clock | A stale world got the correct 503 `features_unavailable` (2026-11-01). After the live loop, the forecast was fresh (feature date 2026-11-03 on 2026-11-04) | [deployment.md](../evidence/deployment.md), [live-loop.md](../evidence/live-loop.md) |

The cloud runs **no always-on SLO evaluation**. The ops monitor is a daily job at 03:50 UTC,
and services scale to zero, so process snapshots look stale between runs by design. Burn-rate
alerting over multiple windows is not implemented.
