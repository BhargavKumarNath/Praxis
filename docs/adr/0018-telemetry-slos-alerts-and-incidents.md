# ADR 0018: Telemetry, SLOs, alerts and incidents

Status: Accepted (Phase 11). Written 2026-10-25. The SLO targets and alert thresholds in
`configs/ops/` are PRE-REGISTERED (version `2026-10-25.1`) before any drill ran and are pinned
by `tests/ops/test_config.py`; changing one needs an ADR amendment and owner approval.
Amended by ADR 0020 (2026-10-25): metrics now cross processes (control-plane snapshots), the
data / backlog / KPI inputs are fed, and `python -m praxis.ops run` schedules the tick. The
first four limitations below are resolved there; the thresholds are unchanged.

Later changes: amended or extended by [ADR 0022](0022-scale-protocol-sharded-consumers-and-batching.md), [ADR 0023](0023-resilience-suite-and-two-window-alerting.md).

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

## Amendment 1 (2026-11-01, Phase 15 remediation, owner-approved): forecast_freshness 24 h

`forecast_freshness` is tightened from 48 h to 24 h (`configs/ops/slos.toml` version
2026-11-01.1; no other target changed). The original 48 h had no recorded rationale here or
in the Phase 11 evidence. The service's contract has one:

* `forecast_age_hours` = feature lag in days × 24. Healthy is lag 1 (yesterday is the newest
  complete day), which gives 24.
* ADR 0010 (Phase 4, validated by the pre-registered backtest) serves the model only up to
  lag 1. From lag 2 the seasonal fallback serves, and after 7 days nothing does.
* Pricing refuses every non-fresh forecast (`require_fresh_forecast = true`), so at lag 2
  every price move is held (`forecast_stale`).

Under 48 h, a forecast service already on its fallback, with pricing frozen, raised no
ticket for a whole day. The Phase 15 row `feature_stale` exposed this as a FAIL (fault seed
801, `docs/evidence/phase-15-failures.json`, kept).

Rejected alternative: serve the model up to 48 h. That would use it outside its validated
serving contract (every horizon shifts by a day, and the h = 1 target is already past) and
would let prices move on 2-day-old features. It relaxes the decision-science requirement
to make an alert quieter.

The 24 h SLO is stricter, never looser. It now breaches exactly when the service stops
serving the model, which `tests/ops/test_config.py` enforces against the forecast config.
`feature_stale` is re-judged on the remediation suite (`configs/resilience/remediation.toml`).
