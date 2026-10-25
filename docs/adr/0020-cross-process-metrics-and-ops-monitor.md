# ADR 0020: Cross-process metrics, pull collectors and the ops monitor

Status: Accepted (2026-10-25, Phase 11 completion before Phase 13). Amends the limitations of
ADR 0018 ("the reader is in-process"; unfed data / business / backlog gauges; no scheduler)
and of ADR 0019 (the agent's static "not wired" labels). Alert thresholds in `configs/ops/`
are unchanged and still pinned.

## Context
ADR 0018 evaluated SLOs and alerts on ONE process's in-memory metrics. Praxis runs as several
processes (API, event consumers, pricing cycle, MLOps pipeline), so no process could see the
system, and the Phase 13 dashboard gate ("reads real system state", "stale data visibly
marked") could not be met. Four alert inputs had no producer at all (source age, contract
failures, KPI deviation, backlogs), and nothing ran `OpsMonitor.tick`. Constraints: local,
GBP 0, no new dependency or infrastructure product (CLAUDE.md s14, s18).

## Decision
* **Push: per-process snapshots in the control plane.** Each process publishes its
  `MetricsView` to `ops_metric_snapshots` (migration 0008), one row per (component,
  instance), overwritten each time, with its own `expires_at`. Long-running loops (API,
  consumers, the monitor) publish every 15 s (monitor: its interval) with a TTL of four
  intervals; batch jobs (pricing `decide`, MLOps `run-day` / `operate`) publish once at the
  end with a 26 h TTL (daily cadence plus slack); the chaos harness publishes once (1 h).
  Readers merge the unexpired rows: counters and histograms stay per-instance series (so
  totals and quantiles add up), the newest gauge per name and labels wins. Expired rows are
  never merged; they are reported as stale. Points not in the metric catalogue are dropped and
  counted. A publish failure never breaks the host process and backs off one interval.
* **Pull: collectors over the stores of record** (`praxis.ops.collect`), run at each read:
  `data_source_age_s` (newest observation per source, warehouse, read-only),
  `data_contract_failures_total` (quarantined batches retrieved in the last 24 h, 0 for every
  source), `dlq_backlog` (open dead letters per subscription, control plane, 0 once drained),
  `business_kpi_deviation{kpi="daily_revenue_<ccy>"}` (last complete ledger day vs the median
  of up to 28 days before it, at least 7; SYNTHETIC ledger, simulated days). One unavailable
  store marks only its collector `unavailable`. These windows were fixed before any run and
  are part of this ADR; changing them needs an amendment.
* **`queue_backlog`** comes only from a broker that can count it (the in-memory harness). The
  Pub/Sub emulator exposes no backlog, so the `consume` loop reports none; on Cloud Pub/Sub it
  would come from Cloud Monitoring (owner approval for any spend).
* **One reader** (`praxis.ops.system.SystemReader` / `build_reader`) feeds the ops monitor,
  `GET /v1/ops/health`, `/ops/dashboard` and the agent's read tools, and returns its inputs:
  telemetry scope, each process's age and staleness, each collector's status.
* **Scheduler:** `python -m praxis.ops run --interval 60` (the ops monitor) ticks, persists
  incidents, publishes its own snapshot, prunes rows expired for a day, and survives a
  database outage (`tick_failed`, next tick). Locally it is a long-running process; on cloud it
  would be a Cloud Run job or Cloud Scheduler trigger (not provisioned).
* Layering: the store lives in `praxis.control` (reachable by streaming, pricing, mlops, api,
  ops, agent); collectors live in `praxis.ops` (may read `control` and `data`).

## Alternatives rejected
* OTel Collector + Prometheus (or Cloud Monitoring locally): standard, but a new
  infrastructure product to run and secure for a single-machine, GBP 0 setup; the exporter
  remains the cloud path and can be attached to the same providers (ADR 0018).
* Re-deriving everything from the databases: impossible for HTTP latency, inference outcomes
  and consumer lag, which exist only in process.

## Consequences / limitations
* It is a latest-value table, not a time series: SLO windows are still each process's
  lifetime, and a restarted process starts its counters again (the old row expires).
* A process that stops is visible only as stale; its last counters leave the view at expiry.
* Reads open the DuckDB warehouse read-only; while an ingest holds the writer lock the
  warehouse collector reports `unavailable` for that read.
* KPI days are the ledger's simulated days; with a finished simulation the KPI does not move.
* Measured once (pre-registered, 2 runs per arm, alternated): telemetry costs about 32% of
  event-chaos throughput (median 2,137 ev/s real vs 3,120 ev/s with telemetry stubbed). This
  ADR adds one throttled clock check per pull, not per event. Reducing the per-delivery cost
  (e.g. aggregating in `StreamMetrics` and flushing to OpenTelemetry) is left to Phase 14.
