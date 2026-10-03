# ADR 0001: Pub/Sub instead of Kafka

Status: Accepted (Phase 0)

## Context
Praxis needs a durable event transport with DLQs and replay on a £0 to £5 monthly budget.
The project is cloud-native on GCP and has no requirement for Kafka-specific semantics
such as compacted logs or consumer-group offset control.

## Decision
Use Google Pub/Sub as the primary event transport. Treat delivery as at-least-once,
possibly delayed and out of order. Every consumer is idempotent and keyed on `event_id`.
Archive raw events in GCS so replay does not depend on Pub/Sub retention.

## Consequences
* No broker to operate; scale-to-zero pricing with a free tier.
* Correctness never relies on exactly-once or ordering; ordering keys are optional.
* Offset-style replay is replaced by replaying the GCS archive.
* Kafka would add operational burden with no current requirement; revisit only if a
  later requirement needs it.
