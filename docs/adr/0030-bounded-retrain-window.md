# ADR 0030: Bounded retrain window (sliding 364 days)

Status: Accepted (2026-11-05, owner-delegated). Amends ADR 0017 (lifecycle policy
`configs/mlops/demand.toml`, `[validation] max_training_days`) and resolves the open item of
ADR 0027 amendment 2.

## Context
Every retrain trains on the whole history and must pass the rolling-origin backtest, with one
origin a week (`require_backtest_acceptance`). Both the number of origins and each origin's
training set grow with the world's age, so a retrain costs roughly the square of the history.
This was measured in the live loop: about 1,600 s at 196 days and about 2,000 s at 203 days,
growing about 400 s a week (ADR 0027 amendment 2). The job's step budget is 14,100 s, so
without a bound the daily loop would eventually time out and the cost would keep rising.

## Options
1. Cap the number of backtest origins. The training set still grows, so memory and fit time
   keep rising.
2. **Sliding training window** (chosen). Both training and backtest use the last N days.
   Cost and memory are constant once the world is older than N.
3. More CPU per retrain. This only moves the wall.

## Decision
`max_training_days = 364`: 52 whole weeks, so the weekly seasonality is intact and a year of
growth or trend is seen. `DemandLifecycle._train` loads `[end - 363, end]`. When the history
is shorter, it loads everything, which is unchanged behaviour. The panel's real start is
written into the artifact manifest (`training.panel_start`), so validation reproduces the
same `data_version` (`validation.training_data`). The policy rejects
`max_training_days < min_training_days`.

## Effect on evidence
None. Every world evaluated so far is at most 252 days long (forecast 196, pricing / MLOps
drills 252, recovery 180, experiments 56), so the window equals the full history for each.
The live world started 2026-04-06, so the cap takes effect for retrains after about
2027-04-05. From then on, a retrain costs about what a one-year retrain costs. By the measured
growth that is below the step budget, but it is an extrapolation, not a measurement. Pinned
by `tests/mlops/test_config.py` (re-pinned with this ADR). `tests/mlops/test_pipeline.py`
covers the window.
