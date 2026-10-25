# Runbook: Stale model or persistent drift

Alerts: model_drift, forecast_stale

## Trigger

The champion has a persistent drift alarm (`model_drift_alarm` = 1) or the served forecast is older than 48 hours.

## Impact

Forecast accuracy and interval coverage degrade; the optimiser freezes when forecast width exceeds its policy limit.

## Diagnose

1. `uv run python -m praxis.mlops status` shows champion, shadow and the last monitoring report.
2. Read the latest drift report: which signal (`residual_drift`, `segment_drift`, `calibration_drift`) and since when.
3. Check feature freshness first (see `external-source-outage`); stale inputs look like drift.

## Mitigate

1. Request retraining: `uv run python -m praxis.mlops request-retraining --as-of <date>`. This only creates a request (ADR 0005); it never replaces the champion.
2. The candidate goes through validation, shadow and an approver. Promotion is a separate, human-approved action.
3. If the champion is clearly harmful, `python -m praxis.mlops rollback` (approver only).

## Verify

1. `model_drift_alarm` returns to 0 after the new champion, or the incident is resolved with a note on why the alarm was noise.
2. `forecast_age_hours` under 48.

## Escalate

Owner for any promotion or rollback.
