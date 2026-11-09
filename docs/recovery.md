# Payment recovery

When a payment fails, Praxis decides when to retry. A survival model estimates the chance an
invoice is cured by each day, the planner schedules up to two retries (three charges in all) by
expected value, and Cloud Tasks runs them. If the model is missing, stale or broken, a fixed schedule decides and
the reason is recorded. Decision: ADR 0015. Results: [results.md](results.md#payment-recovery).

```text
payment.failed / .succeeded (from the payments inbox)
        | Pub/Sub subscription "dunning"
        v
DunningService: one transaction for the idempotency row, invoice lock, case, decision,
                stage and retry job
        |  RecoveryDecider: the model's plan, or the baseline with a recorded reason
        v  after commit (outbox)
TaskQueue.enqueue (task name = hash of invoice, attempt and decision)
        |  LocalTaskQueue locally, Cloud Tasks in the cloud; at least once
        v
POST /v1/tasks/payment-retry -> RetryExecutor
        guard (case state, provider re-fetch, lateness) -> executing (commit)
        -> gateway.pay_invoice with one idempotency key per invoice + attempt
        -> the provider's next notification closes the loop
```

## Models

| Model | Role |
| --- | --- |
| Mixture-cure Weibull, interval-censored MLE | Champion: P(cure by day t) |
| Monotone LightGBM + isotonic calibration | Challenger for first retries |
| Reason x gap rate table, retry +3 d, +7 d, then suspend | Baseline and fallback |

The simulator randomises retry gaps over {1, 2, 3, 4, 5, 7, 10} days, which is what makes the
effect of timing identifiable. Features (`recovery_features.v1`) are built point-in-time from
the control-plane event log.

## Rules

- Praxis must be the only retrier: Stripe Smart Retries stay off in the sandbox.
- The planner never retries after day 20 or beyond `max_attempts`, and the database refuses a
  second live job or a second charge for the same attempt.
- `PRAXIS_RECOVERY_MODEL_DIR` must name one artifact directory. A directory of artifacts is
  refused (never "the newest"), and an artifact older than 45 days falls back to the baseline.

## Commands

```bash
make recovery-data recovery-train recovery-evaluate RC_SEED=1   # development world, about 6 min
python -m praxis.science recovery --db DB --sim SIMDIR --scenario TOML --models ROOT --out DIR
```

The task endpoint needs `PRAXIS_TASKS_TOKEN`, `PRAXIS_TASKS_TARGET_URL` and
`PRAXIS_TASKS_SERVICE_ACCOUNT`, plus a database and a Stripe sandbox key; it answers 503 until
they are set.
