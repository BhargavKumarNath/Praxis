# Runbook: Severe forecast error or calibration regression

Alerts: calibration_regression

## Trigger

80% interval coverage fell below 0.70 on the monitoring window, or forecast error versus the seasonal baseline degraded sharply.

## Impact

Intervals are too narrow to trust; the optimiser's uncertainty gates under-fire. Treat price moves as higher risk.

## Diagnose

1. Read the latest drift report (`python -m praxis.mlops status`): vWAPE ratio, worst slice, coverage.
2. Check whether an upstream data problem or a regime change (real shock or a known simulator scenario) explains it.
3. Compare against the seasonal-naive baseline: a model that no longer beats it has failed its acceptance contract.

## Mitigate

1. Put pricing in `shadow` (policy mode) or rely on the freeze rules until the model is trusted again.
2. Request retraining (`stale-model` steps); never widen intervals by hand to hide the regression.
3. Roll back if a recent promotion coincides with the regression.

## Verify

1. Coverage back inside [0.70, 0.90] on a fresh window; alert clears.
2. Evidence recorded in the retraining request.

## Escalate

Owner before any pricing mode change.
