# MLOps: registry, continuous training, drift and safe promotion (Phase 10)

Decision record: [ADR 0017](adr/0017-model-registry-continuous-training-and-safe-promotion.md)
(extends ADR 0005). Package: `src/praxis/mlops/`. Policy: `configs/mlops/demand.toml`
(pre-registered, hash-pinned). All data is SYNTHETIC.

## What runs

```text
            daily, one data day at a time (praxis.mlops.pipeline.DemandLifecycle.run_day)
 probation ──► shadow evaluation ──► drift monitor ──► retraining
   │                │                     │                 │
   │ worse than     │ failed ─► rejected  │ residual /      │ train Phase 4 recipe
   │ previous ─►    │ passed ─► READY     │ segment /       │ register (candidate)
   │ ROLLBACK       │  (waits for a       │ calibration /   │ validate ─► challenger | rejected
   │ (restore)      │   named approver)   │ PSI / schedule /│ challenger ─► shadow (if free)
   ▼                ▼                     │ labels ─► request
 audit rows     audit rows                ▼
                                    retraining_requests
```

Nothing in the loop promotes a new champion. `promote` needs a named approver and a passing
shadow report against the current champion.

| Stage | Who moves it there | Gate |
| --- | --- | --- |
| candidate | registration (training job or operator) | complete lineage |
| challenger / rejected | automation | offline validation (every check) |
| shadow | automation (one at a time) | a champion exists |
| champion | named approver (bootstrap: validated challenger, no champion) | latest shadow passed vs the current champion |
| retired | the promotion that replaced it | - |
| rolled_back / champion again | probation (automatic) or operator rollback | probation: >= 7 matured origins, vWAPE > 1.05 x previous |

## Commands

```bash
make mlops-data ML_SEED=1                  # steady + drift worlds (1K customers, 252 days)
make mlops-drill ML_SEED=1                 # the pre-registered lifecycle drill (local Postgres)

DB=postgresql+psycopg://praxis@127.0.0.1:55432/praxis_mlops
.venv/bin/python -m praxis.mlops --database-url $DB migrate
.venv/bin/python -m praxis.mlops --database-url $DB --warehouse W --models M \
    register --artifact data/models/demand/<version> --backtest <backtest.json>
.venv/bin/python -m praxis.mlops ... validate --version V --as-of 2026-07-20
.venv/bin/python -m praxis.mlops ... --actor <you> promote --version V --as-of 2026-07-20
.venv/bin/python -m praxis.mlops ... run-day --as-of 2026-07-21     # or operate --start --end
.venv/bin/python -m praxis.mlops ... --actor <you> rollback --as-of D
.venv/bin/python -m praxis.mlops ... status | history | sync-tracking
```

Serving from the registry: `PRAXIS_FORECAST_FROM_REGISTRY=true` + `PRAXIS_DATABASE_URL`;
`/v1/forecasts/demand/model` shows `served_from`, `stage`, `shadow_version`; every response
carries `X-Praxis-Model-Version`. MLflow mirror: `PRAXIS_MLFLOW_TRACKING_URI` (e.g.
`sqlite:///data/mlflow/mlflow.db`; browse with `uvx mlflow ui --backend-store-uri ...`).

## Validation checks (candidate -> challenger)

`artifact_integrity`, `lineage_complete`, `feature_contract`, `dataset_reference`,
`leakage_training_window`, `leakage_future_invariance`, `backtest_acceptance` (pinned Phase 4
acceptance re-evaluated on the report's metrics), `baseline_comparison`, `calibration_80`,
`calibration_50`, `segment_regression_vs_baseline`, `champion_non_inferiority` (backtest
skill vs seasonal naive at most 1.02 x the champion's; amendment 2026-10-17, ADR 0017),
`latency_budget`, `memory_budget`,
`reproducibility`, `serving_parity`. A failed integrity check stops the run; every other
check always runs, so a report lists every reason.

## Drift signals

| Signal | Kind | Fires when (trailing 14 days of matured out-of-sample forecasts) |
| --- | --- | --- |
| residual_drift | decision | pooled vWAPE > 1.30 x the champion's backtest vWAPE |
| segment_drift | decision | worst region / product / segment slice > 1.50 x its backtest slice |
| calibration_drift | decision | 80% interval coverage outside [0.65, 0.95] |
| feature_drift | investigation | a monitored feature's PSI > 0.25 and above the no-change bound |
| prediction_drift | investigation | the scaled point forecast's PSI > 0.25 and above the bound |

Retraining triggers also include `scheduled` (data age >= 28 days) and `new_labels`
(>= 1512 matured targets). Investigation-only triggers wait while a retrained version is
still being evaluated; decision signals do not. One live request per model, 7-day cooldown.

## Limits (read before relying on it)

* Only the demand forecaster is registered. Recovery / customer artifacts and the pricing
  input builder still load fixed directories.
* Production forecasts are re-scored point in time from the warehouse; no log of served
  forecasts exists yet (Phase 11).
* PSI's noise bound assumes independent distinct values; rolling-window features are
  autocorrelated, so feature drift can fire on slow, real-but-harmless changes. That is why
  it is an investigation signal.
* Canary is refused by the policy loader (no canary evaluation exists).
* The Pub/Sub, Cloud Run and Cloud Scheduler wiring of the daily job is not deployed
  (local Postgres + DuckDB + MLflow on SQLite; cloud cost GBP 0).
