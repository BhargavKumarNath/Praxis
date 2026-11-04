# Runbook: Stale model or persistent drift

Alerts: model_drift, forecast_stale

## Trigger

The champion has a persistent drift alarm (`model_drift_alarm` = 1) or the forecast features are older than 24 hours (`forecast_age_hours` > 24: the newest complete feature day is before yesterday). From that point the forecast service serves the seasonal fallback instead of the model (ADR 0010), and the pricing engine holds every price (`forecast_stale`, `require_fresh_forecast`); after 7 days no forecast is served at all (`inference_failures`).

## Impact

Forecast accuracy and interval coverage degrade; the optimiser freezes when forecast width exceeds its policy limit. A stale forecast freezes all price moves until features are fresh again: no price is guessed, but the business runs on yesterday's prices.

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
2. `forecast_age_hours` at or under 24 and responses report `freshness_status` = `fresh`.

## Escalate

Owner for any promotion or rollback.
