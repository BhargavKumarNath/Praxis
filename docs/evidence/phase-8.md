# Phase 8 Evidence Report

```text
Phase: 8 - Payment Recovery and Dunning Intelligence
Date: 2026-10-09
Code revision: 105c930 + working tree (uncommitted; no commits made by Claude)
Environment: Linux, Python 3.12, Postgres 17 (local Docker), Pub/Sub emulator (local Docker),
             DuckDB + dbt (payment marts), LightGBM, SciPy (L-BFGS-B), Terraform via Docker
All customers, payments and recoveries are SYNTHETIC. No Stripe request was made in this
phase and no cloud resource was created or used (Cloud Tasks / Pub/Sub only declared).
```

Design: `docs/recovery.md`, ADR 0015 (states, models, identification, scheduling, amendment).
Raw reports: `phase-8-eval-seed42-evaluation.json` (held-out, run once),
`phase-8-eval-seed42-train.json`, `phase-8-artifact-manifest.json`,
`phase-8-dev-seed1-evaluation.json` (development world).

## Gate

**PASS** (2026-10-09). Every project_plan Phase 8 gate item has evidence; the pre-registered
held-out evaluation (seed 42, built and evaluated once, after an owner-approved amendment made
on the development world only) passed all 14 checks.

| project_plan Phase 8 gate | Result |
| --- | --- |
| transition tests cover every allowed and forbidden state change | PASS: dunning case 16 allowed + every forbidden (state, trigger) pair; retry job 6 allowed + every forbidden pair; tables equal a hand-written spec; terminal states absorb; access never relaxes without payment; Hypothesis: random trigger sequences stay on the machine, a job executes at most once. Postgres trigger seeded with the closure (test keeps them equal) and rejects forbidden moves (`tests/dunning/test_states.py`, `test_service.py`, `tests/control/test_migrations.py`) |
| no duplicate payment retry side effects | PASS: one idempotency key per (invoice, attempt); duplicate events (`processed_events`) create no second job; duplicate / re-delivered tasks are no-ops (charge count unchanged); a crash after `executing` re-dispatches with the same key (one charge); the database refuses a second live job per invoice and a second possibly-charged job per attempt; end-to-end journey re-delivered completely (every notification + the task) leaves charges and state unchanged |
| calibration measured | PASS (held-out): champion Brier 0.1648 (rate table 0.1659, base rate 0.2149), log loss 0.5009, PR-AUC 0.651 (table 0.626, prevalence 0.308), ECE 0.034 vs perfect-calibration noise p95 0.064; reliability curve in the report; segments (2 reasons, 3 tiers with >= 80 rows) all within tolerance |
| survival assumptions checked | PASS: gap assignment uniform (p 0.84) and independent of reason (p 0.63) = non-informative censoring by design; parametric fit vs the model-free current-status estimate in all 17 (reason, gap) cells; censoring handled by the interval likelihood (53.5% of evaluation episodes right-censored); no future leakage (property test + point-in-time extract test); concordance reported only (0.668 survival, 0.578 classifier); P(C <= t) vs TRUE mixture: gated reasons max error 0.033 (<= 0.06) |
| retry policy evaluated on business metrics | PASS (held-out, closed-loop replay vs latent cure times, 445 episodes): net value +GBP 47.72 per episode vs baseline, paired 95% CI [+21.98, +75.99]; recovery rate 61.1% vs 54.6%; recovered revenue x1.057; failed retries per episode 0.84 vs 1.22; restricted days per episode 2.1 vs 5.3; captured 98.5% of oracle value vs 93.6%; mean time to recovery 9.8 vs 6.9 days (reported trade-off) |
| scheduled retry cancellation works | PASS: a recovery event cancels scheduled jobs and deletes their tasks; churn closes cases and cancels jobs; a failed queue deletion cannot cause a charge (executor re-checks the case and re-fetches the provider invoice: `already_paid`, `case_closed`, `invoice_closed`, `attempt_already_made`); reschedule = supersede + new task name; expired dispatch -> re-plan |
| recovery model has a fallback baseline | PASS: model unavailable / corrupt / stale (> 45 days) / features unavailable / unknown category / invalid output -> the deterministic baseline, reason recorded on the decision; held-out replay: fallback schedule identical to the baseline on all 445 episodes; a directory of artifacts is never auto-promoted |

required_test.md s14 retry-scheduler list: schedule, cancel, reschedule, duplicate scheduling
request (`ALREADY_EXISTS`), expired task, already recovered customer, max attempts (planner and
baseline never exceed 3 charges; DB CHECK on attempt number) all have tests.

## What was built

* Simulator (opt-in `billing.recovery`, hash-neutral: golden checksums unchanged): latent cure
  probability and Weibull cure time per failed invoice driven by reason and LATENT reliability,
  absorbing collectibility, persistent reason, randomised retry gaps {1,2,3,4,5,7,10} (the
  identification strategy), `RecoveryTruth` observer. Pre-registered world
  `recovery_eval.toml` (8,000 customers x 180 days).
* `praxis.domain.dunning`: dunning-case and retry-job machines.
* `praxis.recovery` (model layer, never imports the simulator): leakage-safe features,
  point-in-time warehouse extract, episodes and censoring intervals, monotone LightGBM +
  isotonic classifier, rate-table baseline, mixture-cure Weibull with interval-censored MLE
  (analytic gradient), metrics, expected-value retry planner, decider with fallback,
  checksummed artifact with full lineage, pre-registered training protocol, CLI.
* `praxis.dunning` (new layer above payments): Postgres repo, `DunningService` (one
  transaction per event, outbox), `TaskQueue` / `LocalTaskQueue` / `CloudTasksQueue` (REST,
  OIDC), `RetryExecutor`, stream consumer, cloud wiring; API `POST /v1/tasks/payment-retry`.
* `praxis.science.recovery` + `python -m praxis.science recovery`: truth replay of baseline,
  champion, challenger, fallback and oracle; pre-registered acceptance.
* Migration `0004`; mart `fct_invoices` + `dim_customer.tenure_days_at_start`; Pub/Sub
  subscription `dunning`; Terraform module `tasks` (validated, not applied); settings
  `PRAXIS_TASKS_*`, `PRAXIS_RECOVERY_MODEL_DIR`; Makefile `recovery-{data,train,evaluate}`;
  `nightly-science` gains the recovery dev world.

## Tests executed

* `make check` (== CI): see "Final run" below.
* New suites: `tests/dunning/` (states 97, tasks 6, service 12, executor 10, end-to-end 2,
  API / wiring 7), `tests/recovery/` (features incl. leakage property, survival incl.
  gradient check and parameter recovery on a known process, classifier / isotonic / metrics,
  planner golden + optimality vs brute force + closed-loop property, decider fallbacks,
  training protocol / artifact integrity / CLI on a small real world, science replay /
  evaluation / CLI / refusals, training-serving feature parity on 40 real episodes,
  pre-registered file pins), `tests/simulator/test_recovery_truth.py` (events agree with
  latent cure times, gap design, hash neutrality, config validation).
* Development world (seed 1) end to end, then held-out seed 42 once (`make recovery-data
  recovery-train recovery-evaluate`).

## Statistical metrics (held-out seed 42)

* Training: 2,007 episodes before day 120 (1,960 first retries); selection on 191 out-of-time
  rows: survival log loss 0.458 vs classifier 0.501 -> champion `survival_cure_weibull`
  (`recovery-surv-d12545184bfc`).
* Evaluation (days 120-160, 445 episodes; see the gate table). Per-reason truth error
  (survival): card_declined 0.033, insufficient_funds 0.028 (gated); expired_card 0.083 (63
  episodes), processor_error 0.053 (38), authentication_required 0.137 (18) reported.
* Challenger (classifier) policy: net value BELOW the baseline (captured 90.2% vs 93.6%);
  truth error 0.214: first retries only identify t <= 10 days, so the classifier is flat
  beyond. Recorded as a reported result: the survival model's use of all retries matters.

## Failures found and fixes (all before seed 42)

* Pre-registration error (owner-approved amendment, ADR 0015): ECE <= 0.03 is unattainable at
  ~500 rows (a perfectly calibrated model fails 87% of the time; median 0.041) -> "consistent
  with perfect calibration" gate; truth gate restricted to reasons with >= 100 episodes. On
  seed 42 the raw ECE was 0.034, i.e. the ORIGINAL gate would have failed.
* Survival ridge applied to the per-observation mean (an accidental prior ~6x stronger than
  intended) -> fixed to a weak prior on the summed likelihood; Weibull derivative overflow
  (inf x 0) -> stable form; gradient check added.
* Policy-safety bug found in review: pointing the model setting at a directory would have
  used the newest artifact (silent promotion) -> only an explicitly named artifact is used.
* Pre-existing Phase 7 test date bomb: `tests/payments/test_{store,processor}.py` used a fixed
  `NOW = 2026-10-07 12:00` while inbox rows become due at the DB clock -> 13 tests failed once
  the wall clock passed it; `NOW` is now wall clock + 1 day.
* Emulator tests hard-coded 6 Pub/Sub resources -> 7 with the dunning subscription.

## Known limitations

* Development world fails the truth gate (insufficient_funds error 0.13): risky payers churn
  out over time, so later failures cure more often than the training window shows; the model
  under-predicts there. Seed 42 did not show the effect strongly (0.028). Nightly runs the dev
  world with this single documented known failure (`benchmarks/recovery_dev_known_failures.json`);
  any other failing check turns nightly red. Phase 9/10 drift monitoring should own it.
* The replay does not feed failed retries back into simulated churn (Phase 9).
* The model policy recovers later on average (9.8 vs 6.9 days) because it waits for late
  cures that the baseline gives up on; costed by the delay term, reported.
* Small reasons (< 100 evaluation episodes) are reported, not gated; authentication_required
  error 0.137 on 18 episodes.
* Survival family = generating family per customer (mitigated: reliability is latent).
* Cloud Tasks and the dunning Pub/Sub subscription are declared, never applied; no deployed
  worker. Stripe Smart Retries must be disabled manually before Praxis retries Stripe
  invoices; suspension does not yet write anything to the provider.

## Cloud cost incurred

GBP 0 (local Docker and DuckDB only; no Stripe or GCP calls).

## Final run (2026-10-09, code final, after seed 42)

* `make check`: exit 0. ruff + format, mypy (291 files), import contracts 5/5 kept (new layer
  `praxis.dunning`; `praxis.recovery` and `praxis.dunning` added to "models never import the
  simulator" and the web-framework contract), **1,400 passed, 2 skipped** (opt-in live
  BigQuery and Stripe), coverage 99%, 141 per-file floors met (new: `dunning/*` 95,
  `recovery/*` 95), event chaos smoke vs oracle, schemas, secret scan, pip-audit / npm audit
  clean, frontend, Terraform validate, actionlint + zizmor.
* `make nightly-perf`: exit 0 (simulator 72.6K ev/s, peak 109 MB; chaos 2.45K ev/s, 528 MB;
  within the local baseline).
* `make nightly-science`: exit 0. Forecast, elasticity, contamination and pricing dev checks
  pass as before; recovery dev world: only the documented known failure, no new failure.
* `make nightly-security`: **FAILED at gitleaks** with one finding in git history:
  `generic-api-key` on `tests/payments/test_ids_and_model.py:44`, commit `105c930` (Phase 7),
  fingerprint `105c9306cdbe7e74f2b77a5183bbd9d77371c95e:tests/payments/test_ids_and_model.py:generic-api-key:44`.
  Inspected (redacted): the match is the Stripe price lookup key literal
  `praxis_growth_gbp_4900_month`, not a credential. Not caused by Phase 8 and NOT allowlisted
  or rewritten (CLAUDE.md s22/s24): surfaced to the owner. Trivy (the target's second step) was
  run on its own: 0 misconfigurations, including the new `tasks` module.
