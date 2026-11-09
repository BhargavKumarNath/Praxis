# Observability

Praxis watches itself with metrics, traces, SLOs, alerts and incidents. Monitoring sits off the
decision path: if it fails, pricing, payments and MLOps carry on. Decisions: ADR 0018 and 0020.

| What | Where |
| --- | --- |
| Metric catalogue, spans | `src/praxis/telemetry.py` |
| SLOs, alert rules, dashboard panels (pinned) | `configs/ops/{slos,alerts,dashboard}.toml` |
| Evaluators, incidents, health report, runbook validator | `src/praxis/ops/` |
| Cross-process metric snapshots | `src/praxis/control/metric_snapshots.py` |
| Runbooks | [runbooks/](runbooks/README.md) |
| Endpoints | `GET /v1/ops/health`, `GET /ops/dashboard`, `GET /v1/ops/incidents` |

## How metrics reach the alerts

Each process keeps its metrics in memory and publishes a snapshot to Postgres with a TTL that
matches its cadence: the API and consumers every 15 seconds, a pricing cycle or an MLOps run
once per run. Signals no process owns, such as source age, quarantined batches, open dead
letters and the daily payment KPI, are pulled from their stores of record by collectors at read
time. `SystemReader` merges the three. An expired snapshot is never used; it is reported as
stale. A metric with no source is reported as `no_data` with the reason, never as 0 (the
Pub/Sub emulator, for example, has no backlog API).

Metrics are declared in one catalogue (`telemetry.METRICS`), and using an undeclared name or
label raises. Spans for one event or pricing cycle share the Praxis `trace_id`, the same id
stored on the decision record and in the logs.

## SLOs

| SLO | Target |
| --- | --- |
| `api_availability` | >= 99.5 % non-5xx |
| `webhook_processing_success` | >= 99 % durably accepted |
| `pricing_decision_latency_p95` | <= 2,500 ms |
| `event_consumer_lag_p95` | <= 5,000 ms |
| `forecast_freshness` | <= 24 h (tightened from 48 h after the Phase 15 failure, with owner approval) |
| `dlq_backlog` | <= 10 |

There are 16 alert rules across service, data, model and business categories, each with a
runbook. An alert fires only if its condition holds both cumulatively and since the last tick,
so a cumulative ratio can clear once the fault is gone. `make ops-check` fails if an alert's
runbook is missing or incomplete. The resilience suite injects the failure behind
seven of the nine runbooks and checks the alert fires and clears (the other two alerts are
covered by unit tests); two runbooks (`pubsub-backlog`,
`agent-failure`) also have their full procedure executed in a test.

## Incidents

`OpsMonitor.tick()` evaluates the system view, builds the health report and passes firing
alerts to `IncidentManager`: one live incident per alert, a structured-log notification,
auto-resolve after two clear ticks, and manual acknowledge and resolve.

```bash
python -m praxis.ops validate
python -m praxis.ops synthetic-alert --category service [--persist]
python -m praxis.ops run --interval 60       # the monitor; needs PRAXIS_DATABASE_URL
```

A database outage produces a `tick_failed` line and the loop carries on.

## Limits

- Telemetry costs about 32 % of event-chaos throughput (2,137 vs 3,120 ev/s with it stubbed).
- Fleet metrics are latest values only, with no history.
- In the cloud the ops monitor runs once a day and the services scale to zero, so snapshots
  look stale between runs by design. There is no multi-window burn-rate alerting.
