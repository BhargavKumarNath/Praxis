# Phase 15 remediation evidence (owner-approved, 2026-11-01)

> **Superseded in part (2026-11-01):** the `nightly-perf` FAIL and the unverified BigQuery
> unit recorded below were resolved later; see `phase-15-optimisation.md`. The seed-803
> report this file cites is now `phase-15-remediation-803-failures.json` (byte-identical).
> This record is otherwise kept as written.

Code revision: `2272c3a` plus the uncommitted Phase 15 working tree. All data is SYNTHETIC.
No cloud resource, Terraform apply / destroy, BigQuery query or Stripe call was made; cloud
cost incurred: GBP 0.

The original Phase 15 record is kept unchanged:
* `phase-15.md`;
* `phase-15-failures.json` (fault seed 801, 10 / 12, gate FAIL);
* `configs/resilience/failures.toml`.

This file reports the remediation of its three open problems and the re-verification.

## Gate summary after remediation

| Gate item (project_plan.md Phase 15) | Result | Evidence |
| --- | --- | --- |
| Failure tests pass | **PASS: 12 / 12** on fresh fault seed 803 (final code; one run) | `phase-15-remediation-failures.json` |
| No critical dependency vulnerabilities | PASS: pip-audit 0; npm (prod) 0; gitleaks 0 (one fingerprint-scoped ignore); trivy 0 | `make nightly-security` |
| Infrastructure can be destroyed / recreated | PASS with mocked providers only (no real apply) | `make tf-test` |
| Demo mode stays within budget | PASS as an estimate only (not a measured bill) | `docs/cost-guard.md` |
| Runbook set is complete | PASS | `make ops-check` |
| CI gate `make check` | PASS (exit 0) | Tests executed |
| Nightly performance regression (`make nightly-perf`) | **FAIL**: chaos throughput below the 0.5 × baseline floor (cause below) | `make nightly-perf` |

## 1. Feature freshness: 48 h SLO vs the 24 h service limit

**What was wrong.** The Phase 11 `forecast_freshness` SLO allowed 48 h. The forecast
service serves the model only while the newest complete feature day is at most 1 day old;
that is the ADR 0010 serving contract, validated by the Phase 4 backtest. From 2 days the
service serves the seasonal fallback.

Pricing refuses every non-fresh forecast (`require_fresh_forecast = true`), so at lag 2 all
price moves are held. Between 24 and 48 h the decision system was therefore degraded and
frozen with no ticket.

The 48 h has no recorded rationale in ADR 0018 or in the Phase 11 evidence.

**Decision: 24 h** (ADR 0018 amendment 1). The SLO now equals the service's fresh limit:
`forecast_age_hours` = lag × 24, which is 24 in healthy operation.
* The change is a tightening; nothing was relaxed.
* `slos.toml` is now version 2026-11-01.1, and only this target changed.
* A new test pins `max_value == fresh_max_lag_days × 24` against the forecast config, so the
  two cannot drift apart again.
* The `stale-model` runbook now describes the pricing freeze.

Rejected alternative: serve the model up to 48 h. That would use it outside its validated
contract (every horizon shifts by a day) and let prices move on 2-day-old features.

**Result (seed 803):**
* At lag 2 days the API serves `fallback_baseline` and `forecast_stale` fires on the first
  tick (v1: never).
* At lag 8 days the API answers 503 and `inference_failures` fires on the first tick.
* With fresh features again, the model serves and both alerts clear in 2 ticks.

## 2. Consumer crash: healthy messages dead-lettered by other messages' crashes

**What was wrong.** Pub/Sub counts a delivery attempt for every lease that ends without an
ack, so a crashed process costs an attempt to every message it holds. The warehouse
consumer leased and processed whole pulls as one atomic batch, and a crashed batch came
back as one group. At a 1 % crash rate, 1,305 healthy messages reached the DLQ (seed 801),
even though the redrive restored the state.

**Change (ADR 0025).** Both pull loops share `FlowControl` / `process_pull`:
* slow start: one lease after (re)start, doubling per completed pull;
* first deliveries, then first redeliveries, as separate atomic groups, each acked on
  completion;
* a message redelivered twice is processed alone, and the next pull leases one message.

The broker's dead-letter policy (5 attempts), the alerts, the runbook and the redrive are
unchanged. `CrashPlan` gained a `poison` mode (a crash on every delivery). Unit tests cover
the window, the grouping (with a property test), `process_pull`, the Pub/Sub pull sizes,
and scenarios 09b (a transient crash storm dead-letters nothing) and 09c (a process-killing
poison is dead-lettered alone).

**Pre-registration and runs.** The criteria changed in one place: the v1 `consumer_crash`
row expected the crash storm "to dead-letter in-flight messages", which is the collateral
itself.

`configs/resilience/remediation.toml` amends only that row, with a written `amendment`
field:
* isolated crashes must give **zero** dead letters;
* the storm becomes a genuine poison storm (12 `payment.failed` events kill both consumers
  on every delivery). Exactly those 12 must be dead-lettered on each subscription, with no
  healthy message, and `dlq_backlog_high` must page on tick 1.

The other 11 rows are verbatim from v1, and the run section differs only in its seed
(enforced by `test_config.py`). It was pinned before any run on seed 802.

1. **Fault seed 802, single run: 12 / 12 PASS** (`phase-15-remediation-802-failures.json`,
   kept). That run used flow-control version A: any redelivery reset the window and was
   processed alone.
2. `make nightly-perf` then measured version A at **433 chaos ev/s** (floor 1,218). I
   redesigned the flow control for that throughput regression, on development data only.
   ADR 0025 lists all seven variants with their dead-letter and throughput figures; three
   were rejected as unsafe because they dead-lettered healthy messages on development seeds.
3. Because the 802 evidence no longer described the final code, the identical criteria
   were re-registered on fresh fault seed 803 (`remediation-803.toml`, ADR 0023
   amendment 2), pinned before the run, and **run once: 12 / 12 PASS**.

| Seed 803, `consumer_crash` | Measured |
| --- | --- |
| Isolated crashes (1 % before + 1 % after the side effect, 12,115 events) | 5 operational and 244 warehouse crashes; **0 dead letters**; state == oracle |
| Poison storm | 57 / 59 crashes; dead letters 12 on each subscription, **0 healthy** |
| Detection | `dlq_backlog_high` on tick 1 |
| Recovery | fix deployed, then the runbook redrive of 24 letters: state == oracle; 0 open; cleared in 2 ticks |
| Loss / duplicate side effects | 0 / 0 |

**The perf gate fails: a measured trade-off.** A/B against HEAD (`git archive`), on a fresh
database each time, `run-local` with 1,000 customers × 28 days:

| Workload | HEAD | Remediated |
| --- | --- | --- |
| No crashes (healthy path) | 2,385 ev/s | 2,383 ev/s |
| `nightly-perf` chaos (0.5 % + 0.5 % crashes per message, 10 % duplicates, 600 s delays, reorder) | 2,046 ev/s | 932 ev/s |

The cost is the many small DuckDB writes (about 3 ms fixed cost per call) needed to lease
suspects alone during crash recovery. In this chaos workload HEAD produced no dead letters;
the benefit shows at the resilience suite's rates, and with poison.

I did **not** re-record the baseline (CLAUDE.md s24). Owner decision: accept the trade-off
and re-record the chaos baseline with this justification, or fund a faster warehouse sink
first.

## 3. gitleaks finding

`generic-api-key` matched `PLAN.lookup_key == "praxis_growth_gbp_4900_month"` in
`tests/payments/test_ids_and_model.py:44`. That value is a Stripe price lookup key: a
public, human-chosen product label (tier, currency, amount), not a credential.

* **Fixture corrected without losing coverage.** The test now compares
  `(PLAN.lookup_key, PLAN.product_key)` with named expected values. `gitleaks dir` on the
  file reports no leaks.
* The finding is in commit `105c930`, which stays in history (rewriting history needs your
  instruction). `.gitleaksignore` therefore holds exactly one fingerprint (commit, file,
  rule, line) with the reason. No path, rule or later commit is exempt, and the scan
  configuration is unchanged.
* Verified: without the ignore file, the history scan still reports exactly this 1
  finding; with it, 0. `make nightly-security` exits 0.

## 4. Infrastructure and cost safety (no real operation)

* **BigQuery quota unit.** Checked against Google's page
  ([custom quotas](https://docs.cloud.google.com/bigquery/docs/custom-quotas), read
  2026-11-01):
  * it confirms the metric `bigquery.googleapis.com/quota/query/usage` and shows the
    Console value in TiB per day;
  * the quota is approximate, not a hard cap, resets at midnight Pacific Time, and covers
    on-demand pricing only;
  * it does not state the unit the Terraform / Service Usage `override_value` uses, so the
    module's MiB assumption remains **unverified**. It is now the first blocking item in
    the pre-apply checklist (`docs/cost-guard.md`).
* IAM, secrets, destroy guards and cost were reviewed in that checklist. No real apply or
  destroy was made, and none can be without the owner. The budget alert is a notification,
  not a cap.

## Tests executed

| Command | Result |
| --- | --- |
| `make resilience` (seed 803, final code) | exit 0: 18 passed; report 12 / 12 PASS, gate PASS |
| `make resilience` (seed 802, flow control A) | exit 0: 12 / 12 PASS (superseded, kept) |
| `pytest tests/streaming tests/scale` | 165 passed (before the final redesign); `tests/streaming` 147 passed (final code) |
| `make nightly-security` (final code) | exit 0: pip-audit 0, npm prod 0, gitleaks 0 (1 fingerprint ignored), trivy 0 |
| `make nightly-perf` (final code) | **exit 2**: chaos 954 ev/s < floor 1218 (baseline 2436); simulator within bounds |
| `make -k check` (final code) | **exit 0**: 2029 passed, 2 skipped (opt-in live suites), 23.5 min; lint, typecheck (461), arch (7 contracts), ops-check, coverage 98.84 % with floors OK (219 files), schemas, secrets, audit, frontend 66, tf 6 / 6, workflow-lint |

## Remaining limitations / unverified assumptions

* Crashes are in-process exceptions (`ConsumerCrashed`), not OS kills. Most of the suite
  runs on the in-memory broker; Pub/Sub is the emulator. Redelivery order on managed
  Pub/Sub differs.
* The flow control has no hard bound below 5 attempts (ADR 0025, residual risk); 0 healthy
  dead letters is measured, not proven.
* The chaos perf floor fails (above); the healthy-path throughput is unchanged.
* Terraform is proven only against mocked providers; the BigQuery override unit is
  unconfirmed; the cost is an estimate.
* The mismatch in one manual perf measurement came from reusing an un-reset database; the
  same variant matched its oracle on a fresh database at 300 and 1,000 customers. It is not
  fully explained and is recorded here.
