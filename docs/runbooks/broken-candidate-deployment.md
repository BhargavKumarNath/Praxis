# Runbook: Broken candidate or champion deployment

Alerts: inference_failures

## Trigger

More than 5% of model inferences are `unavailable` (artifact missing, checksum or contract mismatch, serving parity failure).

## Impact

Forecast API answers 503; pricing decisions become UNAVAILABLE / HOLD for affected products.

## Diagnose

1. `GET /v1/forecasts/demand/model` shows the serving version and lifecycle stage; compare with `python -m praxis.mlops status`.
2. `model_inference_total{outcome=unavailable}` with logs filtered by `model_version`.
3. Was a promotion or force-promotion in the last hour? (`python -m praxis.mlops history`).

## Mitigate

1. Roll back to the previous champion: `uv run python -m praxis.mlops rollback`. Rollback is audited and approver-only.
2. Never edit the registry rows by hand.
3. Reject the broken version with a reason so it cannot be promoted again.

## Verify

1. `inference_failures` clears; `X-Praxis-Model-Version` shows the restored version.
2. Open a retraining request only after the cause is understood.

## Escalate

Owner immediately if rollback fails or the previous champion's artifact is also unreadable.
