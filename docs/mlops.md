# MLOps

The demand forecaster lives in a model registry with an explicit lifecycle. Every day the
pipeline checks the champion on probation, runs shadow evaluations, monitors drift, and
retrains and validates a candidate when it has to. It never promotes one. Promotion needs a
named approver and a shadow comparison that passed. Decision: ADR 0017 (extending ADR 0005).
Policy: `configs/mlops/demand.toml` (pinned). Results: [results.md](results.md#mlops-lifecycle).

```text
                daily, one data day at a time (DemandLifecycle.run_day)
 probation ──> shadow evaluation ──> drift monitor ──> retraining
   │ worse than      │ failed -> rejected    │ alarm -> request    │ train, register (candidate)
   │ previous ->     │ passed -> ready,      │                     │ validate -> challenger | rejected
   │ ROLLBACK        │ waits for a person    │                     │ challenger -> shadow
   ▼                 ▼                       ▼
                          audit rows, retraining_requests
```

| Stage | Who moves it there | Gate |
| --- | --- | --- |
| candidate | The training job or an operator | Complete lineage |
| challenger / rejected | Automation | Every offline validation check |
| shadow | Automation, one at a time | A champion exists |
| champion | A named approver | The latest shadow passed against the current champion |
| retired | The promotion that replaced it | |
| rolled back | Probation (automatic) or an operator | At least 7 matured origins with vWAPE above 1.05x the previous champion |

## Validation (candidate to challenger)

Integrity and lineage, the feature contract, the dataset reference, two leakage checks, the
pinned forecasting acceptance, the baseline comparison, 50 % and 80 % calibration, segment
regression, non-inferiority to the champion's recipe on identical origins, latency and memory
budgets, reproducibility, and serving parity. A failed integrity check stops the run; every
other check always runs, so a report lists every reason for a rejection.

## Drift

A decision signal over its threshold on one day is a breach. It becomes an alarm, which can
trigger retraining, only after two consecutive evaluated days of the same champion.

| Signal | Kind | Breach (trailing 14 days of matured forecasts) |
| --- | --- | --- |
| residual_drift | decision | Pooled vWAPE > 1.30x the champion's backtest |
| segment_drift | decision | Worst slice > 1.50x its backtest slice |
| calibration_drift | decision | 80 % coverage outside [0.65, 0.95] |
| feature_drift | investigation | A feature's PSI > 0.25 and above the no-change bound |
| prediction_drift | investigation | The forecast's PSI > 0.25 and above the bound |

Retraining is also requested on a schedule (data 28 days old) and when enough new labels have
matured. One live request per model, with a 7-day cooldown.

## Commands

```bash
make mlops-data ML_SEED=1     # steady and drift worlds (1K customers, 252 days)
make mlops-drill ML_SEED=1    # the pre-registered lifecycle drill, about 100 min

DB=postgresql+psycopg://praxis@127.0.0.1:55432/praxis_mlops
python -m praxis.mlops --database-url $DB migrate
python -m praxis.mlops --database-url $DB --warehouse W --models M \
    register --artifact data/models/demand/<version> --backtest <backtest.json>
python -m praxis.mlops ... validate --version V --as-of D
python -m praxis.mlops ... --actor <you> promote --version V --as-of D
python -m praxis.mlops ... run-day --as-of D         # or operate --start D --end D
python -m praxis.mlops ... --actor <you> rollback --as-of D
python -m praxis.mlops ... status | history | sync-tracking
```

To serve from the registry, set `PRAXIS_FORECAST_FROM_REGISTRY=true` and
`PRAXIS_DATABASE_URL`. Every forecast response carries `X-Praxis-Model-Version`. An optional
MLflow mirror is enabled with `PRAXIS_MLFLOW_TRACKING_URI`.

In the cloud, the live loop runs this lifecycle once a day and files a pending promotion
request when a shadow version passes; see [deployment.md](deployment.md#the-live-loop).

## Limits

- The drift signals compare error with the champion's own backtest, so they cannot tell real
  change from a world that simply becomes harder to forecast. Under compounding customer
  growth a frozen champion's error rises 1.4x and the monitor alarms in a way retraining
  cannot fix. That is why the drill's steady world has no organic growth.
- Detection is slow on a gradual ramp: 11 to 13 days after onset, against a 14-day deadline.
- Only the demand forecaster is registered. The recovery and customer models load fixed
  directories.
- Canary deployment is not implemented, and the policy loader refuses it.
