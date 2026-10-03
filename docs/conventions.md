# Logging, Trace and Correlation Conventions

Implemented in `src/praxis/logging.py` and `src/praxis/tracing.py`.

## Logging

* One JSON object per line on stdout.
* Required keys: `timestamp` (UTC ISO-8601), `level`, `logger`, `message`, `service`,
  `trace_id`, `correlation_id` (null outside a trace context).
* Add domain context through `extra={...}`: `event_id`, `entity_id` (synthetic),
  `model_version`, `policy_version`, `decision_id`.
* Keys containing `secret`, `password`, `token`, `api_key`, `authorization` are
  redacted. This is a backstop: never pass secrets to a logger in the first place.
* Never log full payment details, API keys or webhook signing secrets.
* Important production behaviour must also be a metric or audit record, not only a log
  line.

## Trace and correlation IDs

* `trace_id`: 32 lowercase hex, non-zero (W3C `traceparent` / OpenTelemetry compatible).
* `correlation_id`: canonical lowercase UUID, one per business flow.
* HTTP: `X-Correlation-ID` is accepted if valid, otherwise replaced; always echoed on the
  response. Error responses include the correlation ID and no stack trace.
* Events: `trace_id` and `correlation_id` are mandatory envelope fields.
  `causation_id` links an event to its direct cause.
* Consumers must re-bind the envelope's IDs with `trace_context(...)` before processing.
* Malformed inbound IDs are rejected or replaced, never propagated.
