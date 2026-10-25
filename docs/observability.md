# Observability, SLOs and incidents (Phase 11, ADR 0018)

All numbers produced locally are from the SYNTHETIC system; nothing here is a real-world
business result.

## Where things are
| What | Where |
| --- | --- |
| Metric catalogue, spans, in-process reader | `src/praxis/telemetry.py` |
| SLOs / alert rules / dashboard panels (versioned, pinned) | `configs/ops/{slos,alerts,dashboard}.toml` |
| Evaluators, incidents, health report, runbook validator | `src/praxis/ops/` |
| Incident tables | migration `0006_ops_incidents` |
| Runbooks | `docs/runbooks/*.md` |
| Endpoints | `GET /v1/ops/health`, `GET /ops/dashboard`, `GET /v1/ops/incidents` |
| CLI | `python -m praxis.ops validate`, `python -m praxis.ops synthetic-alert --category data` |

## Metrics (catalogue in `telemetry.METRICS`)
Service: `http_requests_total`, `http_server_errors_total`, `http_request_duration_ms`,
`external_calls_total`, `external_call_duration_ms`, `webhook_events_total`.
Event backbone: `events_consumed_total`, `event_lag_ms`, `queue_backlog`, `dlq_backlog`.
Data: `data_source_age_s`, `data_contract_failures_total`.
Model: `model_inference_total`, `model_inference_duration_ms`, `model_version_info`,
`forecast_age_hours`, `model_drift_alarm`, `model_calibration_80`.
Decisions / business: `pricing_decisions_total`, `pricing_frozen_decisions_total`,
`pricing_decision_duration_ms`, `price_change_fraction`, `business_kpi_deviation`.
AI operator (Phase 12, ADR 0019): `agent_tool_calls_total{tool, tier, status}` (`tool` is a
declared tool name or `undeclared`, so hostile names cannot grow cardinality) and
`agent_investigations_total{provider, outcome}`. Neither is in an SLO or alert rule (the agent
is off the decision path); agent failure is detected from the run outcome and its audit trail.

Adding a metric = a `_spec(...)` line in the catalogue; using an undeclared name or label raises.

## Spans
`event.publish` (ingestion) -> `event.consume` (consumer) -> `pricing.cycle` ->
`model.inference`, `optimiser.optimise`, `persistence.record`; plus `external.stripe`. All spans
of one event / cycle share the Praxis `trace_id` (the same id stored on the decision record and
in structured logs). Producer and consumer spans are siblings in the trace (span ids do not
cross the bus).

## SLOs and alerts
See `configs/ops/slos.toml` and `alerts.toml` (each alert names its runbook). Categories:
service, data, model, business. An alert fires when ANY matching series violates the rule.

## Incident flow
`OpsMonitor.tick()` evaluates the current metrics, builds the health report, and passes the
firing alerts to `IncidentManager`: one live incident per alert, a structured-log
notification, auto-resolve after two clear ticks, manual acknowledge / resolve. Try it:
`python -m praxis.ops synthetic-alert --category service` (add `--persist` for Postgres).

## Runbooks
`pubsub-backlog`, `external-source-outage`, `stripe-webhook-surge`, `stale-model`,
`broken-candidate-deployment`, `database-outage`, `severe-forecast-error`, `agent-failure`,
`business-kpi-shock`. `make ops-check` fails if an alert's runbook is missing or incomplete.
Two procedures are executed end to end in tests: `pubsub-backlog`
(`tests/ops/test_runbook_drill.py`) and `agent-failure`
(`tests/agent/test_runbook_agent_failure.py`); the others are structure-checked only, and the
agent's `get_runbook` tool says so.

## Limits
See ADR 0018 "Consequences / limitations".
