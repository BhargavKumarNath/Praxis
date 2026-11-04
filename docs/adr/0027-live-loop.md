# ADR 0027: The live loop (one synthetic day per real day, model lifecycle on a schedule)

Status: Accepted (2026-11-04, owner-instructed: "we NEED to run it live"; owner choices:
promotion stays a named human's, the live data is a simulated clock, a scheduler within the
GBP 10 ceiling). Extends ADR 0017 (lifecycle), ADR 0020 (ops monitor), ADR 0026
(deployment). Evidence: `docs/evidence/live-loop.md`.

## Context
After ADR 0026 the deployment ran, but nothing in it moved on its own:
1. **No new data.** The image carried a fixed seed-1 warehouse ending 2026-09-13, so the
   forecast answered 503 by the freshness contract (ADR 0010).
2. **No trigger.** The lifecycle (`praxis.mlops run-day`: probation, shadow, drift, retrain)
   existed and passed its drill (ADR 0017), but nothing ran it.
3. **A fixed model.** The API served a baked artifact, not the registry's champion.

project.md asks for a system that continuously receives data, detects change and adapts
safely. There is no real data source (Stripe is off, data is synthetic by contract), so the
environment has to be the simulator, run as a clock.

## Decision
* **A live world, one simulated day per real UTC day.** `configs/simulator/scenarios/
  live.toml` (seed 9001, 1,000 customers, day 0 = 2026-04-06), pre-registered before any
  tick. Each tick re-runs it with `--days` = the days observed so far, so a day appears only
  after it has happened.
  * `population.arrival_horizon_days` is fixed (730), which makes the world **prefix-stable**:
    a longer run reproduces every earlier day exactly (`tests/live/test_world.py`), so a
    rebuild never rewrites history the lifecycle already monitored. Event / trace ids are
    run-scoped (they hash the config, which includes the length) and so change between
    ticks; the batch warehouse is rebuilt whole and does not depend on them.
  * Organic change is left on (default growth, compounding), and one sustained regime change
    is written into the scenario in advance (us_east demand x1.4 from 2026-11-30), so a
    drift detection is not chosen after the fact. It is an operating environment, not an
    evaluation world: nothing measured on it is evidence for a pre-registered gate.
* **The live tick (`python -m praxis.live tick`, a Cloud Run job).** Rebuild the world
  through yesterday (simulator -> raw load -> dbt marts), publish the warehouse, then
  `praxis.mlops operate` for every day not yet operated (at most 7 per execution; the next
  tick continues), then, if a shadow version has passed, `praxis.agent investigate
  review_candidate` (the deterministic triage; no LLM is configured) files a PENDING
  promotion request. State and a report per tick go to the live volume; a failed step stops
  the tick without advancing the state, so the next tick retries the same days.
  * `praxis.live` only sequences the existing, tested CLIs as subprocesses; an import-linter
    contract forbids it from importing the lifecycle, the agent, pricing, payments or
    simulator ground truth. Promotion can therefore never happen inside the tick.
  * **Bootstrap** (`praxis.live bootstrap --approver NAME`, once): train on the world
    through 2026-08-30, backtest acceptance, register, validate, and a named human promotes
    the first champion (the registry's existing bootstrap rule). The days from 2026-08-31 to
    today are then operated by ordinary ticks on point-in-time data, so the loop goes live
    with real monitoring history instead of an empty one.
* **Promotion is unchanged (ADR 0005, ADR 0017).** Automation retrains, validates, moves a
  challenger into shadow and rolls back a regressing champion on probation; a new champion
  needs a named human approving the operator's request in the console. The API serves the
  registry's champion (`PRAXIS_FORECAST_FROM_REGISTRY`) and picks up an approval or a
  rollback within 30 s, with no redeploy. configs/mlops/demand.toml is not changed.
* **The live volume.** One private, versioned Cloud Storage bucket mounted (Cloud Storage
  FUSE, Cloud Run gen2) at `/mnt/live`: read-write by the live job only, read-only by the
  API, the console and the ops monitor. The same path everywhere means a registered
  artifact's `file://` URI resolves in every process. The warehouse is replaced by copy +
  rename; versioning keeps the previous generation readable for 2 days while a reader still
  holds it. Nothing is baked into the image any more (no model, no warehouse).
* **Schedule.** Cloud Scheduler: the live tick at 03:10 UTC and one ops-monitor tick at
  03:50 UTC (so drift and calibration gauges reach the alert rules), no retries. A
  dedicated scheduler identity holds `roles/run.invoker` on exactly those two jobs and no
  secret; the module refuses more than 3 triggers or more than one run a day.
* **Identities.** `live` reads the database URL only and writes only the live bucket;
  `scheduler` reads nothing. No project-level role is added (the spend guard's stays the
  single classified one). The mlops CLI now reads `PRAXIS_DATABASE_URL` when
  `--database-url` is absent, so no secret is ever passed on a command line; the image
  records its build revision (`PRAXIS_CODE_REVISION`) for model lineage.
* **dbt in the image.** `dbt-core` + `dbt-duckdb` move to a `pipeline` dependency group
  (installed by the image, included in dev); `dbt-bigquery` stays dev-only.

## Cost (estimates; Billing is the measured signal)
* Live job: one execution a day, 4 vCPU / 8 GiB (amendment 2; was 2 / 4), measured
  durations in the evidence, at most 4 h (was 30 min). A few minutes a day plus one long
  retrain day a week comes to about USD 0.5-3/month outside any free tier, growing with the
  history (amendment 2).
* Live bucket: about 0.2-1 GB Standard in us-central1 (5 GB Free Tier), a few hundred
  operations a day.
* Scheduler: 2 jobs (3 free per billing account; at most USD 0.20/month otherwise).
* The GBP 10 budget and the GBP 8 spend guard are unchanged and still cover everything.

## Alternatives rejected
* **Promotion by automation.** The contract forbids it without a separately defined and
  tested policy (CLAUDE.md s10); the owner chose human approval.
* **An always-on consumer fleet or a streaming service.** Recurring cost for no gain: the
  world produces a day at a time, so a daily batch is the honest cadence.
* **Generating the whole future world once and releasing it day by day.** Equivalent
  output, but the future would exist on disk; re-running the deterministic world up to
  yesterday keeps "not yet happened" literally true and costs about 30 s a day.
* **Baked warehouse + daily redeploy.** A deploy per day, and the lifecycle would still
  need persistent artifacts for the registry.
* **BigQuery as the feature store.** The marts and features are DuckDB-native; moving them
  is a separate decision with its own cost line.

## Consequences
* The forecast is fresh (features through yesterday) and served by whatever the registry
  says is champion; retraining, validation, shadow and rollback happen without anyone.
* A new champion still waits for a human: the console shows the pending promotion request.
* One simulated day per real day: a full retrain-shadow-promote cycle takes weeks of real
  time (28-day schedule trigger, 14 matured shadow origins). The bootstrap's operated
  history shows the cycle has already happened once.
* The streaming plane (Pub/Sub consumers) is still exercised only on demand; the live world
  feeds the analytical plane and the lifecycle, not the event bus.

## Amendment 1 (2026-11-04): a 60-minute execution budget

**What failed.** Catch-up tick 7 (execution `praxis-dev-live-f54vk`) operated 2026-10-12 to
10-18. That range holds the scheduled + drift retrain of 2026-10-13, and the `operate` step
hit the 1,500 s step timeout and was killed. The state was not advanced: no day was skipped,
and the next tick retried the same days. The timeout surfaced as a raw traceback rather than
a failed step.

**Cause.** Capacity, not logic. The job has 2 vCPU and runs the bootstrap backtest about
4.6x slower than the development machine (605 s vs 130 s), and a later retrain trains on a
longer history. The 30-minute budget was set before any cloud measurement.

**Change.**
* The live job's timeout is 3,600 s and the step timeout 3,300 s. The job module's bound
  rises from 30 to 60 minutes.
* A timed-out step is an ordinary `StepFailed` (returncode 124, the output tail logged), so
  the tick exits 1 cleanly (`tests/live/test_loop.py::test_a_step_that_times_out_is_a_failure_not_a_crash`).

No acceptance criterion, threshold or lifecycle policy changes: this is an operating budget,
and `configs/mlops/demand.toml` and `configs/live/*.toml` are untouched.

**Cost.** The worst case is one 60-minute execution a day at 2 vCPU / 4 GiB, about USD 0.16
a day, still far inside the GBP 10 ceiling. In practice only a retrain day (about weekly) is
long, and ordinary days take a few minutes.

**Rejected.** More vCPU (the speed-up of the lifecycle's training is unmeasured; the budget
fix is the smaller change). A smaller `max_catchup_days` (a single retrain day alone must
fit, so it does not help, and the file is pinned).

## Amendment 2 (2026-11-04): retrain cost grows with the world; 4 vCPU and a 4-hour budget

**What was measured.** With amendment 1 in place, the catch-up retrain days took about
1,600 s (2026-10-20, tick `trl6c`: 1,794 s total) and about 2,000 s (2026-10-27, tick
`sw4hk`: 2,228 s total). A 7-day tick without a retrain takes about 230 s. The growth is
about 400 s a week, so a retrain day would pass the 3,300 s step budget within 2 to 3 weeks
and the daily loop would start failing.

**Cause.**
* Every retrain must pass the rolling-origin backtest (`require_backtest_acceptance`, one
  origin a week). Both the number of origins and each origin's training set grow with the
  history, so the cost grows roughly with the square of the world's age.
* The pinned forecast config trains with `num_threads = 4`, but the job had 2 vCPU.

**Change (capacity only; no policy, threshold or config changes).**
* The live job gets 4 vCPU and 8 GiB, matching `num_threads`.
* The job timeout is 4 h and the step timeout 14,100 s. The job module's bound rises to 4 h.

**Cost (estimate).** Ordinary days take a few minutes. A retrain day at 4 vCPU is
estimated at about USD 0.1-0.3 today, rising as the history grows, so roughly USD 1-3 a
month after a year of live operation. That is still inside the GBP 10 ceiling, and the
GBP 8 spend guard is unchanged.

**Open (owner decision).** The quadratic growth is a property of the pinned lifecycle
policy, which backtests the whole history at every retrain. A bounded training window, or
a fixed number of backtest origins, would cap the cost. That changes the pre-registered
policy (ADR 0017), so it needs its own ADR and the owner's approval; it is not made here.

## Amendment 3 (2026-11-04): one warehouse file name; a `republish` repair

**What failed.** The first live smoke (`docs/evidence/live-deployment-smoke-run1.json`)
scored 48 / 49. The forecast endpoint answered 500 with DuckDB `Catalog "warehouse" does
not exist`.

**Cause.**
* DuckDB names a database's catalog after its file stem, and dbt-duckdb qualifies its views
  with it. The tick built `.../warehouse.duckdb` and published it as `praxis.duckdb`, so
  every mart view in the published copy pointed at a catalog that did not exist.
* The lifecycle and the review read the work copy, so neither the trial nor the ticks saw
  it. Only a reader of the published copy (the API) could.

**Change.**
* One file name (`praxis.duckdb`) for the built and the published warehouse.
* A test opens the published copy and resolves a catalog-qualified view
  (`test_the_published_warehouse_keeps_its_catalog`).
* `python -m praxis.live republish` rebuilds the world exactly as the last tick saw it and
  publishes it. It operates nothing and leaves the state unchanged; prefix stability makes
  the rebuild identical to what the lifecycle read. It was run once (execution
  `praxis-dev-live-gkmrd`, 152 s) to replace the broken copy.
