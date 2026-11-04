# ADR 0023: Resilience suite, two-window alerting and the detection fixes it forced

Status: Accepted (2026-11-01, Phase 15). Amends ADR 0018 (alert evaluation), ADR 0011 / 0017
(serving telemetry), ADR 0019 (agent provider telemetry) and the Phase 3 `dlq --redrive`
command. No import-linter contract changed; no new dependency.

## Context
required_test.md s21 and project_plan.md Phase 15 list twelve failures (source timeout,
malformed source, DB timeout, Pub/Sub redelivery, consumer crash, service restart, model
missing, stale features, failed candidate, Stripe duplicate, agent unavailable, event burst).
Each must be injected against a real boundary and record: expected behaviour, actual
behaviour, detection time, recovery, data loss, duplicate side effects and the alert raised.

## Decision
1. **Pre-registered criteria.** `configs/resilience/failures.toml` (version 2026-11-01.1)
   holds, per failure, the injection, expected behaviour, recovery, the alerts that MUST fire
   within one 60 s tick, the alerts that must stay quiet, and the signal recorded when no
   alert is expected. It was written and hash-pinned (`tests/resilience/test_config.py`)
   before any failure ran. Bounds are the strictest available: detection on the first tick
   (`max_ticks_to_detect = 1`), clearance within `clear_after_ticks = 2`, zero data loss and
   zero duplicate side effects. Worlds: simulator seed 1 and fault seed 801 (disjoint from
   every held-out seed and from Phase 14's 701).
2. **Harness.** `tests/resilience/harness.py` drives the production ops path
   (`OpsMonitor`-equivalent ticks: `SystemReader` -> `build_health` -> SLOs / alerts ->
   `IncidentManager`), measures each row into a `Measured` record and judges it mechanically
   against its criteria row. `conftest.py` writes every row, including rows that never
   reached the judge ("FAIL (no record)"), to `docs/evidence/phase-15-failures.json`
   (`make resilience`). The same tests run in `make test`, so a regression fails CI.
3. **Two-window alerting (SRE workbook multi-window rule).** Cumulative ratio / latency
   SLOs could never clear after a breach: a cumulative counter remembers the bad events
   forever. An SLO or denominator ratio rule now alerts only when the cumulative view AND the
   recent window (the counter increase since the previous tick, `MetricsView.increase`)
   both violate. Without a recent view (single evaluation, API health call) the old
   cumulative rule applies. `SloStatus.recent_state` is exposed. No threshold changed.
4. **Detection gaps closed (found by analysis before the affected rows ran):**
   * an unservable champion was invisible: `model_serving_refused{model,stage}` gauge plus
     alert `model_unservable` (page, runbook broken-candidate-deployment);
   * a missing model was not an unavailable inference: the API and `RegistryForecaster` now
     count `model_inference_total{outcome=unavailable}`;
   * agent provider outages had no alert: `agent_provider_failures_total{provider}` plus
     `agent_provider_degraded` (ticket, > 25 % of at least 3 investigations, runbook
     agent-failure); `python -m praxis.agent investigate` publishes an `agent` batch snapshot
     (ADR 0020) so the ops monitor can see it.
5. **Defects found by failing runs, fixed and disclosed:**
   * `consumer_crash` attempt 1: one `dlq --redrive` stopped at 1000 letters, so a storm of
     1319 left data unrecovered (data loss 4 at the oracle). `redrive_all` pages until every
     decodable letter is republished; the CLI and `run-local` use it. Tested by
     `tests/streaming/test_redrive_all.py`.
   * `feature_stale` attempt 1: `forecast_age_hours` was only set after a successful
     forecast, so a stale refusal froze the gauge at its last fresh value. It is now set
     before the refusal. The row still FAILS (below).

## Results (2026-11-01, `docs/evidence/phase-15-failures.json`)
10 of 12 PASS; **the failure-test gate item is not met**.

* `feature_stale` FAIL: the pinned Phase 11 `forecast_freshness` SLO is 48 h while the
  forecast service degrades to its baseline after 24 h (fresh_max = 1 day). At 2 days of lag
  the API serves `fallback_baseline` with no ticket; `forecast_stale` does not fire within
  one tick of the injection. Only the 8-day refusal pages (`inference_failures`). Proposed
  (owner approval required, not applied): align the SLO with the service's fresh limit
  (24 h), justified by the service contract, not by this result, then re-test on a fresh
  world.
* `consumer_crash` FAIL against its pre-registered expectation ("isolated crashes are ...
  applied once"): a 1 % crash rate made the batched warehouse consumer dead-letter 1300
  innocent batch-mates (5 operational), because a crash abandons the whole leased batch and
  every redelivery burns an attempt. This is the collateral ADR 0009 already recorded;
  the runbook redrive restores the oracle exactly (0 loss, 0 duplicates) and
  `dlq_backlog_high` pages on tick 1. Candidate fix (not applied, needs pre-registration and
  a fresh fault seed): after a crash restart, bisect / process the first redelivered batch
  one message at a time so only the crashing message consumes attempts.

## Consequences
* Every alert a failure should raise exists in `configs/ops/alerts.toml` (version
  2026-11-01.1, 16 rules) with a complete runbook (`make ops-check`).
* The criteria file can only change through an amendment to this ADR with the owner's
  approval; the hash test fails otherwise.
* Limitations: the Pub/Sub row uses the emulator (not the managed service: no real ack
  deadline jitter or regional failover); crashes are in-process (`ConsumerCrashed`), not
  SIGKILL of an OS process; Stripe duplicates are re-signed locally (no Sandbox call).

## Amendment 1 (2026-11-01, owner-approved remediation): remediation suite, fault seed 802

The v1 result above stands, and so do `configs/resilience/failures.toml` and
`docs/evidence/phase-15-failures.json` (both unchanged and still hash-pinned).

The fixes were justified independently of the failed rows:
* ADR 0018 amendment 1: the SLO equals the service's fresh limit;
* ADR 0025: crash-isolating flow control.

The remediation suite `configs/resilience/remediation.toml` (v2026-11-01.2) was
pre-registered and pinned before any run on fault seed 802. `test_config.py` enforces that
it changes nothing except:
* `run.fault_seed` (801 → 802);
* the `consumer_crash` row.

The v1 row expected the crash storm "to dead-letter in-flight messages", which was the
collateral itself. The amended row keeps "isolated crashes applied once" and states it as
zero dead letters. It replaces the transient storm with a genuine poison storm: 12
payment.failed events kill both consumers on every delivery. Exactly those 12 must be
dead-lettered on each subscription, with no healthy message, and `dlq_backlog_high` must
page on tick 1.

`feature_stale` keeps its v1 criterion word for word.

All twelve rows run again on seed 802. `make resilience` writes
`docs/evidence/phase-15-remediation-failures.json`.

## Amendment 2 (2026-11-01): re-registration on fault seed 803

The remediation suite ran once on fault seed 802 and passed 12 / 12
(`docs/evidence/phase-15-remediation-802-failures.json`, kept). That run used the first
version of the ADR 0025 flow control.

`make nightly-perf` then measured that version at 433 chaos events/s, against a floor of
1,218. The flow control was redesigned for the throughput regression, on development data
only (ADR 0025 lists every variant). The 802 evidence therefore no longer describes the
final code.

`configs/resilience/remediation-803.toml` (v2026-11-01.3) re-registers the identical
criteria on the fresh fault seed 803. `test_config.py` enforces that it is identical to
registration 1 apart from version, supersedes and seed. It was pinned before any run on
803, and its single run is the evidence for the final code:
`docs/evidence/phase-15-remediation-failures.json`, 12 / 12 PASS.

New resilience seeds start at 804.

## Amendment 3 (2026-11-01): re-registration on fault seed 804

The seed-803 run (12 / 12) is kept as `docs/evidence/phase-15-remediation-803-failures.json`
(renamed, byte-identical). The warehouse write path then changed (ADR 0025 amendment 1;
flow control unchanged).

`configs/resilience/remediation-804.toml` (v2026-11-01.4) re-registers the identical
criteria on the fresh fault seed 804. It was pinned before any run on 804 and is enforced
identical apart from version, supersedes and seed. Its single run gave 12 / 12 PASS
(`docs/evidence/phase-15-remediation-failures.json`). New resilience seeds start at 805.
