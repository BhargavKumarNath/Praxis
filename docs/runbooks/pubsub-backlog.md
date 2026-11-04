# Runbook: Pub/Sub backlog or dead-letter growth

Alerts: queue_backlog_high, dlq_backlog_high, consumer_lag_slo_breach

## Trigger

A subscription has more than 1000 undelivered messages, the DLQ holds more than 10 messages, or consumer lag p95 exceeds 5 s.

## Impact

Downstream state (operational store, warehouse, monitoring projections) lags. Delivery is at-least-once and every consumer is idempotent, so lag is recoverable and replays are safe.

## Diagnose

1. `uv run python -m praxis.streaming dlq` lists dead-letter counts by reason.
2. Decode-type reasons mean a producer broke the contract; NACK or handler-error growth usually means the database (see `database-outage`).
3. `max_delivery_attempts_exceeded` with consumer restarts: a message that crashes the process. Consumers lease repeatedly redelivered messages alone (ADR 0025), so in the measured runs only the crashing messages reached the DLQ; dead letters that share one event type usually point to a bad deploy for that type.
4. Compare publish rate with consumer throughput (`events_consumed_total`, `event_lag_ms`).

## Mitigate

1. Fix the cause first (contract, database, bug).
2. Then redrive: `uv run python -m praxis.streaming dlq --redrive [--reason <reason>]`. Redrive republishes with the original event ids, so consumers deduplicate. It drains every open dead letter in pages of 1000 (`redrive_all`; before Phase 15 one run stopped after 1000, found by `tests/resilience/test_event_failures.py`).
3. Pure load: add consumer capacity within the configured Cloud Run max instances.

## Verify

1. `queue_backlog` and `dlq_backlog` fall below their limits; both alerts clear and the incident auto-resolves.
2. No duplicated business effects (sink counts equal unique event ids).

## Escalate

Owner if the DLQ keeps refilling after redrive (a poison message or a live contract break).
