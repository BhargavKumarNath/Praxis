# Runbook: Control-plane database outage or slow persistence

Alerts: api_availability_slo_breach, decision_latency_slo_breach

## Trigger

API 5xx ratio breaches 0.5%, or pricing cycles exceed the p95 latency target (persistence is the usual cause).

## Impact

Decisions cannot be recorded, therefore none is executable (audit rule, ADR 0013). Webhooks answer 503 and Stripe retries. Consumers NACK transient errors and messages are redelivered.

## Diagnose

1. `make pg-up` locally / Cloud SQL status in the cloud; `psql` connectivity with the configured URL (do not print it).
2. Compare `http_server_errors_total` by route with `external_calls_total` and the persistence spans (`persistence.record`) in traces.
3. Look for connection exhaustion or long locks rather than a full outage.

## Mitigate

1. Restore the database; the system is built to wait: transient errors are retried by redelivery, not dropped.
2. Keep pricing in `shadow` mode until recording works; never bypass the audit record to execute.
3. Do not run manual DDL against the production-like schema; use migrations.

## Verify

1. Availability SLO ok, pricing cycle `audit_complete = true`.
2. No message stuck in the DLQ from the outage window (see `pubsub-backlog`).

## Escalate

Owner if the outage exceeds 30 minutes or any data loss is suspected.
