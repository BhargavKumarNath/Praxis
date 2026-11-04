# Phase 15 evidence: Resilience, Chaos, Security and Cost Hardening

Date: 2026-11-01. Code revision: `2272c3a` plus the uncommitted Phase 15 working tree.
All data is SYNTHETIC (simulator worlds, fixtures, local services). No cloud resource,
BigQuery query or Stripe call was made; cloud cost incurred: GBP 0.

> **Historical record (attempt 1, fault seed 801), kept unchanged.** The owner-approved
> remediation and its fresh-seed re-run (fault seed 802) are reported separately in
> [`phase-15-remediation.md`](phase-15-remediation.md); this file and
> `phase-15-failures.json` are not edited by it.

## Gate summary

| Gate item (project_plan.md Phase 15) | Result | Evidence |
| --- | --- | --- |
| Failure tests pass | **FAIL: 10 / 12 rows pass** (`feature_stale`, `consumer_crash` fail) | `phase-15-failures.json`, ADR 0023 |
| No critical dependency vulnerabilities | PASS: pip-audit 0; npm (prod) 0; npm (all) 0 high / 0 critical, 16 moderate in dev tooling | `make audit`, `npm audit` |
| Infrastructure can be destroyed / recreated | PASS with mocked providers (6 / 6 `terraform test` runs); no real apply | `make tf-test`, ADR 0024 |
| Demo mode stays within budget | PASS as an estimate (about GBP 0-0.20 / month from official list prices, 2026-11-01); not a measured bill | `docs/cost-guard.md` |
| Runbook set is complete | PASS: 16 rules, 9 runbooks, `make ops-check` problems [] | `docs/runbooks/README.md` |

**Phase 15 gate: NOT PASSED** (failure tests: 2 of 12 rows fail against their
pre-registered criteria). Nothing was loosened to change that.

## Protocol (ADR 0023)

* Criteria `configs/resilience/failures.toml` v2026-11-01.1, sha256
  `4810c7db...aef3b14`, written and pinned before any failure ran. Every row: one alert
  tick = 60 s, detection within 1 tick, clearance within 2 ticks, 0 data loss, 0 duplicate
  side effects, named quiet alerts must not fire; rows without an alert name the signal that
  records them. Worlds: sim seed 1, fault seed 801.
* Real boundaries: a local HTTP server behind the production `HttpFetcher` (real sockets and
  timeouts); Postgres `statement_timeout` under a competing `ACCESS EXCLUSIVE` lock; the
  Pub/Sub emulator; the signed Stripe webhook route over the Postgres inbox; the model
  registry with artifacts on disk; DuckDB warehouse files; the production consumers and
  ops path (`SystemReader` -> health -> SLO / alert evaluation -> incidents, Postgres store).
* Judge: mechanical, per row (`tests/resilience/harness.py`); the report lists every row,
  and a row that never reached the judge is FAIL.

## Results (`docs/evidence/phase-15-failures.json`)

| # | Failure | Verdict | Detection | Recovery / clear | Loss / dup | Key measurement |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | source timeout | PASS | source_stale 60 s after the freshness limit | 2 ticks | 0 / 0 | 3 attempts x 0.2 s timeout in 0.60 s; warehouse untouched; no alert while inside the 48 h budget |
| 2 | source malformed | PASS | data_contract_failure 60 s | 2 ticks after the 24 h window | 0 / 0 | quarantined with raw body + checksum kept; normalised rows unchanged |
| 3 | DB timeout | PASS | webhook_success + api_availability SLOs 60 s | 2 ticks | 0 / 0 | 25 / 25 deliveries 503 `inbox_unavailable` (~206 ms each); 0 acked without a durable write; redelivery all accepted |
| 4 | Pub/Sub redelivery (emulator) | PASS | none expected; queue / DLQ alerts quiet | 1 tick | 0 / 0 | 100 deliveries applied then ack lost + 83 events re-published: 183 / 183 recognised as duplicates; state == oracle |
| 5 | consumer crash | **FAIL** | dlq_backlog_high 60 s | 2 ticks after redrive | 0 / 0 | isolated 1 % crashes (9 op, 239 wh) dead-lettered 1,305 innocent batch-mates (expected 0); runbook redrive of 1,319 restores the oracle |
| 6 | service restart | PASS | dlq_backlog_high 60 s | 2 ticks | 0 / 0 | restarted monitor continues the same incident (1 row, notifications opened -> resolved); webhooks accepted before an API restart processed once after it |
| 7 | model missing | PASS | model_unservable + inference_failures 60 s | 2 ticks | 0 / 0 | deleted champion artifact: last good version keeps serving 200; restart / missing dir: 503 |
| 8 | feature stale | **FAIL** | forecast_stale never; inference_failures 60 s at 8 d | 2 ticks | 0 / 0 | 2-day lag: served `fallback_baseline` with no alert; 8-day lag: 503 `features_unavailable` |
| 9 | failed candidate | PASS | none expected; model alerts quiet | n/a | 0 / 0 | corrupted artifact rejected (artifact_integrity); bad recipe rejected (4 checks); champion unchanged; 2 rejection events |
| 10 | Stripe duplicate | PASS | none expected; webhook SLOs quiet | n/a | 0 / 0 | 3 deliveries of one event: 1 inbox row, 1 fetch, 1 internal event; out-of-order pair resolved by refetch; 2 duplicates counted |
| 11 | agent unavailable | PASS | agent_provider_degraded 60 s | 2 ticks | 0 / 0 | 3 runs completed and grounded via deterministic triage; pricing executions identical; 0 guarded executions |
| 12 | event burst | PASS | queue_backlog_high 60 s | 2 ticks | 0 / 0 | 52,745 events + 20 % duplicates published before any consumer: peak backlog 63,356; drained in 18.5 s (~7,000 deliveries/s); state == oracle |

## Failures found / fixes

Disclosed in the order they happened. No criterion, threshold, seed or test was changed
after a result.

1. **Before any affected row ran (analysis):** cumulative ratio SLOs could never clear ->
   two-window alert rule; unservable champion invisible -> `model_serving_refused` +
   `model_unservable`; missing model not counted as an unavailable inference -> counted;
   agent outages unalerted -> `agent_provider_failures_total` + `agent_provider_degraded`,
   agent CLI batch snapshot. (ADR 0023 s3-4.)
2. **feature_stale attempt 1 FAIL** (record kept outside the repo as
   `feature_stale_attempt1.json`): `forecast_stale` never fired. Cause 1 (bug):
   `forecast_age_hours` was set only after a successful forecast, so stale refusals froze the
   gauge. Fixed (set before the refusal). Cause 2 (contract mismatch): the pinned Phase 11
   SLO is 48 h while the service degrades after 24 h. The final run still FAILS on cause 2.
   **Owner decision:** align `forecast_freshness` with the service's 24 h fresh limit
   (ADR 0018 amendment), then re-test on a fresh world.
3. **consumer_crash attempt 1 FAIL**: data loss 4 at the oracle because one `dlq --redrive`
   stops at 1,000 letters. Fixed with `redrive_all` (CLI and `run-local`). Attempt 2 still
   FAILS against "isolated crashes ... applied once": the known ADR 0009 batch collateral.
   A candidate fix (bisect the first redelivered batch after a restart) needs
   pre-registration and a fresh fault seed (802+); not applied.
4. **service_restart attempt 1** errored on a harness bug of mine (the restart test seeded
   fewer than the alert threshold of dead letters and queried a wrong table name); fixed
   in the harness, no criterion touched.
5. **Terraform (mocked test)**: a `for_each` over apply-time service-account emails would
   have failed the first real apply; changed to static keys.

## Security review

* Routes: `tests/security/test_route_inventory.py` pins all 9 API and 13 console routes with
  their protection class; every write refuses an unauthenticated caller (400 / 401 / 403 /
  422 / 503, no trace); no route deletes data or carries a price. `/docs`, `/redoc`,
  `/openapi.json` are now served only in `local`.
* Cloud IAM: per-process service accounts, no project-level or primitive role, no keys,
  only the API public, console private by validation (`tests/security/test_terraform_posture.py`).
* Secrets: Secret Manager containers with per-secret reader lists; values never in
  Terraform state; Cloud Run env names checked against `Settings` fields.
* Scans: trivy config on `infra/terraform` 0 findings (one accepted, GCP-0077, exp
  2027-03-31); pip-audit 0; npm prod 0; gitleaks 1 = the known false positive
  (`tests/payments/test_ids_and_model.py:44`, commit 105c930), **not allowlisted (owner
  decision)**, so `make nightly-security` still exits non-zero.

## Cost

See `docs/cost-guard.md` (Phase 15 section): enforced Cloud Run instance caps and BigQuery
daily quota; a GBP 5 budget alert (not a cap); estimate GBP 0-0.20 / month in demo mode.

## Tests executed

| Command | Result |
| --- | --- |
| `make resilience` | 15 passed, 2 failed (the two FAIL rows); report written |
| `make tf-test` / `make tf-check` | 6 / 6 runs pass; validate OK |
| `make nightly-perf` | exit 0 (simulator 74,912 ev/s, chaos 2,165 ev/s, within baseline bounds) |
| `make nightly-security` | exit 2: gitleaks known false positive only; trivy 0 (run standalone) |
| `make workflow-lint`, `make ops-check` | exit 0 |
| `make -k check` | **exit 2**: only `test` fails (2 failed = the two FAIL rows, 2017 passed, 2 skipped opt-in live suites, 22.5 min); every other gate passes: lint, typecheck (460 files), arch (7 contracts), ops-check, coverage floors (219 files, total 98.84 %), events-check, schemas, secrets, audit, frontend (66 tests), tf-check + tf-test, workflow-lint |

## Known limitations

* Mocked Terraform proves graph correctness, not GCP behaviour; no real apply / destroy.
* Pub/Sub was the emulator; crashes are in-process exceptions, not OS process kills.
* The BigQuery quota unit (MiB/day) is from third-party sources, to confirm on first apply.
* The demo cost is an estimate from list prices.
* Phase 14 1M carry-over untouched.

## Gate

NOT PASSED.

## Reason

`feature_stale` and `consumer_crash` fail their pre-registered criteria. Both causes are
understood and documented with proposed fixes that need the owner's approval
(SLO alignment; batch bisect after a crash, with a fresh seed). All other gate items pass,
two of them only as mocked or estimated evidence (stated above).
