# Phase 11 evidence: Observability, SLOs, Incident Management

Phase: 11
Date: 2026-10-25
Code revision: 25cc36c + uncommitted Phase 11 working tree
Environment: local (Linux, Docker Postgres + Pub/Sub emulator, in-memory broker for drills). SYNTHETIC data only.

Tests executed:
- `make check` (exit 0): 1737 passed, 2 skipped (opt-in live BigQuery / Stripe suites), coverage floors OK (185 files, incl. new floors `ops/*` 95, `telemetry.py` 95, `ops/__main__.py` 85), lint, mypy strict, import-linter (5 contracts kept), `ops-check`, schemas, secrets, audit, workflow-lint, frontend, terraform.
- New tests: `tests/unit/test_telemetry.py`, `tests/ops/` (config pins, evaluators and boundaries, incidents on memory AND Postgres, health/API, metric emission, end-to-end trace, runbook drill, CLI) = 83 tests.
- `make nightly-perf` (exit 0).
- Not run: `nightly-science` (no model or feature code changed; only gauges added to the drift monitor), `nightly-security` (the only dependency change declares two already-installed packages; `make audit` inside `make check` was clean).

Results (s17 + plan gate):
- Metrics emitted by the real code paths: request count, 5xx errors, latency, event lag, queue backlog, model version, inference count, decision count, frozen decision count (`test_metrics_emission.py`).
- Trace: one synthetic event traced across ingestion (`event.publish`), event bus, consumer (`event.consume`), model (`model.inference`), optimiser (`optimiser.optimise`) and persistence (`persistence.record`); all spans share the event's trace id, no other trace appears, parent/child chain consume > pricing.cycle > {model, optimiser, persistence} (`test_trace_e2e.py`). Span status records the exception type only (a secret in the message does not appear).
- Alerts: one alert per category (service `queue_backlog_high`, data `source_stale`, model `calibration_regression`, business `business_kpi_shock`), each with observed value, threshold, series and a runbook path (`test_evaluate.py`).
- Synthetic alert reaches the incident flow in all four categories, on memory and Postgres stores, labelled `[SYNTHETIC]`, linked to the real runbook (`test_incidents.py`, `python -m praxis.ops synthetic-alert`).
- Runbook procedure tested: 12 dead letters -> `dlq_backlog_high` fires -> one page-severity incident with `docs/runbooks/pubsub-backlog.md` -> the runbook's redrive step executed (`redrive_dead_letters`) -> consumers apply all 12 events -> backlog 0 -> incident auto-resolves after 2 clear ticks; timeline `opened, auto_resolved` (`test_runbook_drill.py`). Only this one runbook is executed end to end; the other eight are structure-validated (`make ops-check`), not exercised.
- Dashboards: health report with service / data / model / business sections (JSON + escaped static HTML), empty system reports `no_data` rather than healthy.

Performance metrics:
- `nightly-perf` passed its floors. Simulator 69,576 ev/s, peak RSS 108.9 MB (local baseline recorded earlier: see `benchmarks/perf_baseline.json`). Event chaos run 1,690 ev/s, RSS 593 MB versus local baseline 2,436.5 ev/s / 531.9 MB, i.e. 0.69x throughput and 1.12x RSS. This is within the gate (>= 0.5x, <= 1.5x) but it is a 31% drop. I did not isolate how much is the per-delivery telemetry call versus machine noise (this run was a single sample on a machine that had just finished a 20-minute test run). Baseline NOT re-recorded.

Statistical metrics:
- None: Phase 11 adds no model. Alert thresholds and SLO targets are engineering defaults for the synthetic system, pre-registered in `configs/ops/*.toml` (2026-10-25.1) before any drill, unchanged since.

Failures found:
- First `make check`: 4 mlops tests failed with `KeyError: 'alarms'`.

Fixes:
- Cause: my gauge call (`_emit_model_health`) was inserted after the probation report instead of the drift report (a string-replace hit the wrong site). Moved to the drift report; added a direct test. No threshold, test or gate was changed. Full `make check` re-run once: exit 0.

Known limitations:
- In-process metrics reader: the dashboard shows the serving process only; no exporter, no cloud Monitoring (ADR 0018).
- Queue/DLQ backlog gauges are sampled from the in-memory broker and control-plane store by a helper; nothing samples them automatically in a running deployment yet. Likewise `OpsMonitor.tick` is not scheduled anywhere (CLI/tests only), and nothing feeds `data_source_age_s`, `data_contract_failures_total` or `business_kpi_deviation` from the real ingestion/warehouse paths yet. The alerts for those are tested with injected values only.
- No multi-window burn-rate alerting; SLO window is process lifetime.
- Notification is a structured log (`LogNotifier`); no pager/chat integration.
- Producer and consumer spans are siblings in one trace, not parent/child.
- `agent-failure` runbook is forward-looking (Phase 12). `business-kpi-shock` is an extra ninth runbook.
- Known gitleaks false positive `tests/payments/test_ids_and_model.py:44` not touched (not a Phase 11 issue).

Cloud cost incurred:
- 0 (all local).

Gate:
PASS (with the limitations above; the gate items "dashboards show service/data/model/business health", "synthetic alert reaches incident flow", "runbook procedure tested", "trace follows a decision end to end" are each demonstrated by the named tests).

Reason:
All s17 tests and the four plan gate items pass under `make check` exit 0. The wiring of automatic metric sources (data freshness, contract failures, KPI, backlog sampling) and a scheduler for `tick` is NOT done and should be treated as open work, not as passed.
