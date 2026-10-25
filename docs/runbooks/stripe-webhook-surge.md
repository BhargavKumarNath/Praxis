# Runbook: Stripe webhook surge or failures

Alerts: webhook_success_slo_breach

## Trigger

Webhook acceptance ratio dropped below 99% (400/413/503 responses), or the inbox backlog grows after a burst.

## Impact

Stripe retries with backoff; payment state in the control plane lags. No data is lost while Stripe keeps retrying (events are deduplicated by event id).

## Diagnose

1. Break the `webhook_events_total` counter down by `outcome`: `invalid_signature` means a wrong or rotated signing secret; `inbox_unavailable` means the database; `payload_too_large` an abnormal sender.
2. Never log or paste the signing secret. Compare the endpoint secret in Secret Manager with the Stripe dashboard endpoint.
3. A surge with healthy outcomes is only load: check `http_request_duration_ms` p95 and the inbox processor lag.

## Mitigate

1. `invalid_signature`: rotate / re-sync the endpoint secret, then replay failed deliveries from the Stripe dashboard (idempotent).
2. `inbox_unavailable`: follow `database-outage`.
3. Load only: scale the API (Cloud Run max instances is capped by design; raise it only with owner approval) and let the asynchronous processor drain.

## Verify

1. `webhook_processing_success` SLO back to ok; the alert clears.
2. Inbox has no unprocessed rows older than the retry window.

## Escalate

Owner if signatures keep failing after a secret re-sync (possible misconfiguration or abuse).
