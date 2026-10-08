# Payment recovery and dunning (Phase 8)

**All customers, payments and recoveries are SYNTHETIC.** Decisions: ADR 0015 (states, models,
scheduling, identification, amendment), with ADR 0003 / 0014 (payments) and ADR 0009 (events).
Evidence: `docs/evidence/phase-8.md`.

```text
provider (Stripe / synthetic) --notification--> inbox -> processor -> payment.failed / .succeeded
                                                                          | Pub/Sub sub "dunning"
                                                                          v
DunningService (txn: processed_events, invoice lock, case, decision, stage, retry job)
        |  features: control-plane event log -> recovery_features.v1
        |  RecoveryDecider: champion model plan  |  baseline (fallback, reason recorded)
        v  after commit (outbox)
TaskQueue.enqueue(task name = hash(invoice, attempt, decision))  -- LocalTaskQueue / Cloud Tasks
        |  at run_at, at least once
        v
POST /v1/tasks/payment-retry -> RetryExecutor: guard (case, provider re-fetch, lateness)
        -> executing (commit) -> gateway.pay_invoice(idempotency key per invoice+attempt)
        -> succeeded / failed;  the provider's new notification closes the loop
```

## Packages

| Module | Role |
| --- | --- |
| `praxis.domain.dunning` | Dunning-case and retry-job machines, access levels |
| `praxis.recovery.features` | `recovery_features.v1` (pure; leakage-safe) |
| `praxis.recovery.warehouse` / `dataset` | Point-in-time histories from the marts; episodes and retry intervals |
| `praxis.recovery.classifier` | Monotone LightGBM + isotonic (first retries); reason x gap rate table baseline |
| `praxis.recovery.survival` | Mixture-cure Weibull, interval-censored MLE (analytic gradient) |
| `praxis.recovery.metrics` | Brier, log loss, ECE, reliability, PR-AUC, segments, interval concordance, assignment tests |
| `praxis.recovery.planner` | Expected-value retry planning on the day grid (pure) |
| `praxis.recovery.policy` | `RecoveryDecider`: model policy with baseline fallback |
| `praxis.recovery.train` / `artifact` | Pre-registered protocol; checksummed artifact with full lineage |
| `praxis.dunning.store` / `service` | Postgres cases, decisions, jobs; event handling; outbox; re-plan |
| `praxis.dunning.tasks` | `TaskQueue`, `LocalTaskQueue`, `CloudTasksQueue` |
| `praxis.dunning.executor` | Idempotent retry execution with stale-state guards |
| `praxis.dunning.consumer` / `wiring` | Stream consumer; cloud composition from settings |
| `praxis.api.tasks` | `POST /v1/tasks/payment-retry` (Cloud Tasks HTTP target) |
| `praxis.science.recovery` | Evaluation vs latent cure times (the only place truth is read) |

Postgres (migration `0004`): `dunning_cases`, `dunning_decisions` (append-only), `retry_jobs`
(one live job per invoice, one possibly-charged job per attempt). New mart `fct_invoices`;
`dim_customer` gained `tenure_days_at_start`. New Pub/Sub subscription `...-events-dunning`
and Cloud Tasks queue `praxis-<env>-payment-retries` (Terraform declared, never applied).

## Running it

```bash
make recovery-data recovery-train recovery-evaluate RC_SEED=1   # development world (~6 min)
make recovery-data recovery-train recovery-evaluate             # held-out seed 42: ONCE
.venv/bin/python -m praxis.recovery --db DB train --selection-cutoff D --train-cutoff D \
    --gap-choices 1,2,3,4,5,7,10 --out ROOT --report FILE
.venv/bin/python -m praxis.science recovery --db DB --sim SIMDIR --scenario TOML --models ROOT --out DIR
```

Settings (names in `.env.example`): `PRAXIS_TASKS_TOKEN`, `PRAXIS_TASKS_TARGET_URL`,
`PRAXIS_TASKS_SERVICE_ACCOUNT`, `PRAXIS_RECOVERY_MODEL_DIR`. The task endpoint answers 503
until the database, a Stripe sandbox key, the token, the GCP project and the target are all set;
without a model directory the baseline decides.

## Rules

* Never run development work on seed 42; the recovery acceptance and world are hash-pinned.
* Praxis must be the only retrier: disable Stripe Smart Retries / automatic retries in the
  sandbox (ADR 0015 consequences).
* The planner never retries after day 20 (nothing later is identified) or beyond
  `max_attempts`; the database refuses a second live job or a second charge per attempt.
* Retraining is not promotion (ADR 0005): `PRAXIS_RECOVERY_MODEL_DIR` must name ONE artifact
  directory; a directory of artifacts is refused (never "the newest"), and an artifact older
  than 45 days is ignored (baseline).
