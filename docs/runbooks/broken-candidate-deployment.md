# Runbook: Broken candidate or champion deployment

Alerts: inference_failures, model_unservable

Exercised end to end by `tests/resilience/test_model_failures.py` (Phase 15: missing artifact).

## Trigger

- `inference_failures`: more than 5% of model inferences are `unavailable` (no servable
  model loaded: artifact missing at startup, never-loadable champion, features too stale).
- `model_unservable` (page): `model_serving_refused{stage="champion"} = 1`. The registry's
  champion cannot be loaded (artifact missing, checksum or feature-contract mismatch). The
  last good version keeps serving, so a promotion or a ROLLBACK has silently not taken effect.

## Impact

`inference_failures`: the forecast API answers 503; pricing decisions become UNAVAILABLE /
HOLD for affected products. `model_unservable`: production serves an older version than the
registry says (or nothing, if no version ever loaded); forecasts stay valid but are not the
approved champion.

## Diagnose

1. `GET /v1/forecasts/demand/model` shows the serving version and lifecycle stage; compare with `python -m praxis.mlops status`.
2. `model_inference_total{outcome=unavailable}` with logs filtered by `model_version`.
3. Was a promotion or force-promotion in the last hour? (`python -m praxis.mlops history`).
4. `model_unservable`: logs `mlops.serving_refused` name the refused `model_version`; check that
   the registered `artifact_uri` exists and its checksum matches (`load_registered`).

## Mitigate

1. Roll back to the previous champion: `uv run python -m praxis.mlops rollback`. Rollback is audited and approver-only.
2. Never edit the registry rows by hand.
3. Reject the broken version with a reason so it cannot be promoted again.
4. `model_unservable` from a deleted or moved artifact: restore the exact artifact directory
   (same checksum) from its source, or roll back. Never re-register a different artifact
   under the refused version.

## Verify

1. `inference_failures` clears; `X-Praxis-Model-Version` shows the restored version.
   `model_unservable` clears on the next serving refresh (`model_serving_refused` back to 0).
2. Open a retraining request only after the cause is understood.

## Escalate

Owner immediately if rollback fails or the previous champion's artifact is also unreadable.
