# Observability, SLOs and incidents (Phase 11, ADRs 0018 and 0020)

All numbers produced locally are from the SYNTHETIC system; nothing here is a real-world
business result.

## Where things are
| What | Where |
| --- | --- |
| Metric catalogue, spans, in-process reader | `src/praxis/telemetry.py` |
| SLOs / alert rules / dashboard panels (versioned, pinned) | `configs/ops/{slos,alerts,dashboard}.toml` |
| Evaluators, incidents, health report, runbook validator | `src/praxis/ops/` |
| Incident tables | migration `0006_ops_incidents` |
| Cross-process metric snapshots (store, publisher) | `src/praxis/control/metric_snapshots.py`, migration `0008` |
| Pull collectors, system reader | `src/praxis/ops/collect.py`, `src/praxis/ops/system.py` |
| Runbooks | `docs/runbooks/*.md` |
| Endpoints | `GET /v1/ops/health`, `GET /ops/dashboard`, `GET /v1/ops/incidents` |
| CLI | `python -m praxis.ops validate`, `python -m praxis.ops synthetic-alert --category data` |
| Ops monitor (scheduler) | `python -m praxis.ops run --interval 60` (needs `PRAXIS_DATABASE_URL`) |

## How metrics reach the alerts (ADR 0020)
Every process keeps its metrics in process and PUBLISHES a snapshot to the control plane:

| Component | Publishes | TTL |
| --- | --- | --- |
| `api` (incl. the forecast service) | every 15 s (app lifespan) | 60 s |
| `consumer-<role>` (`python -m praxis.streaming consume`) | every 15 s from the pull loop, and at exit | 60 s |
| `streaming-local` (`run-local` harness), incl. sampled `queue_backlog` | once at the end | 1 h |
| `pricing` (`python -m praxis.pricing decide --database-url ...`) | once per cycle | 26 h |
| `mlops` (`run-day`, `operate`) | once per run | 26 h |
| `ops-monitor` | every tick | 4 intervals (min 60 s) |

Signals no process owns are PULLED from the stores of record by the collectors at each read:
`data_source_age_s` and `data_contract_failures_total` (warehouse, read-only; quarantined
batches of the last 24 h), `dlq_backlog` (open dead letters per subscription) and
`business_kpi_deviation` (last complete day of the SYNTHETIC payment ledger vs the median of
the up-to-28 days before it). `queue_backlog` exists only where the broker can count it (the
in-memory harness); the Pub/Sub emulator has no backlog API.

`SystemReader` merges its own live metrics, the unexpired snapshots and the collectors. Every
health response carries `inputs`: `telemetry_scope` (`fleet`, or `process` without a database),
each process with `age_s` and `stale`, and each collector's status. An expired snapshot is
never used; it is listed as stale (and marked STALE on `/ops/dashboard`).

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
`OpsMonitor.tick()` evaluates the system view (above), builds the health report, and passes
the firing alerts to `IncidentManager`: one live incident per alert, a structured-log
notification, auto-resolve after two clear ticks, manual acknowledge / resolve. Try it:
`python -m praxis.ops synthetic-alert --category service` (add `--persist` for Postgres).
`python -m praxis.ops run` is the scheduler: one JSON line per tick (status, alerts, opened /
resolved incidents, stale processes, collector status); a database outage gives a
`tick_failed` line and the loop carries on.

## Runbooks
`pubsub-backlog`, `external-source-outage`, `stripe-webhook-surge`, `stale-model`,
`broken-candidate-deployment`, `database-outage`, `severe-forecast-error`, `agent-failure`,
`business-kpi-shock`. `make ops-check` fails if an alert's runbook is missing or incomplete.
Two procedures are executed end to end in tests: `pubsub-backlog`
(`tests/ops/test_runbook_drill.py`) and `agent-failure`
(`tests/agent/test_runbook_agent_failure.py`); the others are structure-checked only, and the
agent's `get_runbook` tool says so.

## Limits
See ADR 0018 and ADR 0020 "Consequences / limitations". Measured once: telemetry costs about
32% of event-chaos throughput (2,137 vs 3,120 ev/s with telemetry stubbed); see
`docs/evidence/phase-11-wiring.md`.
