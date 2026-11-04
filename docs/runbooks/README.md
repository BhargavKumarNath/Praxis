# Runbooks

Every alert in `configs/ops/alerts.toml` names one of these runbooks; `make ops-check`
fails if a rule's runbook is missing or lacks a section (trigger, impact, diagnose,
mitigate, verify, escalate). Phase 15 (`tests/resilience/`, ADR 0023) injects each failure
below and checks that the named alert fires on the first 60 s tick and clears after the
runbook's recovery.

| Runbook | Alerts | Exercised by (tests/resilience/) |
| --- | --- | --- |
| [external-source-outage](external-source-outage.md) | source_stale, data_contract_failure | test_source_failures.py |
| [database-outage](database-outage.md) | api_availability_slo_breach, decision_latency_slo_breach | test_webhook_failures.py (db_timeout) |
| [stripe-webhook-surge](stripe-webhook-surge.md) | webhook_success_slo_breach | test_webhook_failures.py (db_timeout) |
| [pubsub-backlog](pubsub-backlog.md) | consumer_lag_slo_breach, queue_backlog_high, dlq_backlog_high | test_event_failures.py |
| [stale-model](stale-model.md) | model_drift, forecast_stale | test_model_failures.py (feature_stale: FAIL, ADR 0023) |
| [broken-candidate-deployment](broken-candidate-deployment.md) | model_unservable, inference_failures | test_model_failures.py |
| [severe-forecast-error](severe-forecast-error.md) | calibration_regression | Phase 11 tests |
| [business-kpi-shock](business-kpi-shock.md) | business_kpi_shock, price_volatility | Phase 11 / 12 tests |
| [agent-failure](agent-failure.md) | agent_provider_degraded | test_agent_unavailable.py |

The authoritative mapping is the `runbook` field of each rule; run `make ops-check` for the
current list.
