# Phase 11 checkpoint: Observability, SLOs, Incident Management (started 2026-10-25)

## State (update at each milestone)
- DONE: `praxis.telemetry` (OTel SDK, catalogue METRICS, Praxis trace-id adoption), instrumentation
  (API middleware, webhooks, producer, StreamMetrics mirror, consumer span, forecast service,
  pricing cycle/optimiser/persistence, Stripe client, mlops monitor gauges), `praxis.ops`
  (config, evaluate, incidents [memory + Postgres, migration 0006], health, monitor, signals,
  runbooks), `configs/ops/{slos,alerts,dashboard}.toml` (PRE-REGISTERED 2026-10-25.1),
  9 runbooks in `docs/runbooks/`, API `/v1/ops/health`, `/ops/dashboard`, `/v1/ops/incidents`.
- Tests so far: tests/unit/test_telemetry.py, tests/ops/{test_config,test_evaluate,test_incidents}.py.
- DONE too: health/API/signals/metrics-emission/e2e-trace/runbook-drill/CLI tests (tests/ops, 78 pass), pyproject layers+floors, Makefile ops-check, migration test 0006.
- ALL DONE: ADR 0018, docs, evidence, make check exit 0. See docs/evidence/phase-11.md.
  (alert -> incident -> runbook procedure -> clear), `python -m praxis.ops` CLI + Makefile
  `ops-drill`, pyproject layers/contracts/coverage floors (ops/* 95; telemetry.py 95), migration
  test head 0006, ADR 0018, docs/observability.md, CLAUDE.md s24, architecture.md,
  evidence phase-11.md, project_progress.md, `make check` once.
- Dependencies: only declare opentelemetry-api / -sdk (already installed via mlflow). No exporter.
- Do NOT change configs/ops thresholds after a drill result (pinned by tests/ops/test_config.py).
