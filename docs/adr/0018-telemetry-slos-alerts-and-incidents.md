# ADR 0018: Telemetry, SLOs, alerts and incidents

Status: Accepted (Phase 11). Written 2026-10-25. The SLO targets and alert thresholds in
`configs/ops/` are PRE-REGISTERED (version `2026-10-25.1`) before any drill ran and are pinned
by `tests/ops/test_config.py`; changing one needs an ADR amendment and owner approval.
Amended by ADR 0020 (2026-10-25): metrics now cross processes (control-plane snapshots), the
data / backlog / KPI inputs are fed, and `python -m praxis.ops run` schedules the tick. The
first four limitations below are resolved there; the thresholds are unchanged.

## Context
Phases 0-10 left "in-process metrics, Phase 11 exports through OpenTelemetry" notes in
`StreamMetrics`, `PricingMetrics` and `LatencySample`. Phase 11 must make Praxis operable as
a service: traces and metrics for the API, Pub/Sub consumers, model inference, the optimiser
and external calls; SLOs; alerts that reach an incident flow; runbooks; health dashboards.
Constraints: local only, no cloud spend, no new infrastructure product (no Prometheus /
Grafana / Datadog), the core system must work with none of this running.

## Decision
* **`praxis.telemetry` (foundation layer)** wraps the OpenTelemetry SDK (already installed via
  MLflow; now declared as direct dependencies, no exporter). A catalogue (`METRICS`) declares
  every metric and its labels; emitting anything else raises, which bounds label cardinality
  and lets alert rules be validated against the catalogue. Providers are instance-local, not
  the OTel globals; an OTLP / Cloud Monitoring exporter can attach to the same providers later.
* **One trace per business flow.** Spans adopt the Praxis `trace_id` (ADR on tracing,
  `praxis.tracing`) as the OTel trace id. A producer publish, the consumer, the model call,
  the optimiser and the decision-store write of one event share that id; the decision record
  already stores it. Span ids are not propagated over the bus (no new event field), so the
  producer span and the consumer span are siblings in one trace, not parent and child.
  Exception text is never put on spans (secrets); only the exception type.
* **Instrumentation points** reuse existing seams: `StreamMetrics.inc/observe` mirrors into
  telemetry (so every consumer outcome and the publish-to-ack lag is covered), the FastAPI
  middleware (route template label), the webhook handler, `ForecastService`, the pricing cycle
  (`pricing.cycle`, `optimiser.optimise`, `persistence.record`), the Stripe client, and the
  MLOps drift monitor (drift alarm, interval coverage).
* **`praxis.ops` (same layer as streaming and the models; imports only foundation, control
  and telemetry).** Pure evaluators turn a metric snapshot into SLO statuses and firing alerts
  (`evaluate.py`). Definitions are versioned TOML: `slos.toml` (ratio, latency quantile by
  histogram bucket upper bound so it is conservative, gauge maximum), `alerts.toml`
  (threshold or SLO-breach rules, each with a runbook), `dashboard.toml`.
* **Incident flow** (`incidents.py`, migration 0006): firing alert -> deduplicated incident
  (one live incident per fingerprint, enforced by a partial unique index) -> notification
  (structured log; pager / chat is a `Notifier` to add later) -> auto-resolution after two
  consecutive clear evaluations, or manual acknowledge / resolve. The incident is durable
  before anyone is notified. Synthetic alerts carry their own fingerprint and a `[SYNTHETIC]`
  label.
* **Health report** (service, data, model, business sections) is a JSON endpoint and a static
  HTML page. It is read-only; turning alerts into incidents is `OpsMonitor.tick`, never a
  side effect of a GET.
* **Runbooks** are markdown with required sections, validated against the alert config in
  `make ops-check` (every alert's runbook exists, lists its alerts, has all sections).

## Consequences / limitations
* The reader is in-process: the dashboard shows the metrics of the process serving it.
  A multi-process deployment needs the exporter step (Cloud Monitoring), which is out of
  scope locally and would need owner approval for spend.
* Queue and DLQ backlog gauges are sampled by a helper from the in-memory broker and the
  control-plane store; on Cloud Pub/Sub they would come from Cloud Monitoring.
* The business KPI shock input is a generic deviation signal (`ops.signals.record_kpi`);
  nothing yet feeds it from the warehouse revenue mart. Values in this repository are
  SIMULATED and must not be read as real business results.
* `source_stale` fires only for sources that report an age; a source that stops reporting
  entirely is not detected by that rule (the data freshness CLI covers it).
* SLO windows are the process lifetime; there is no multi-window burn-rate alerting yet.
* Alert thresholds are engineering defaults for the synthetic system, not tuned to any
  observed distribution.
