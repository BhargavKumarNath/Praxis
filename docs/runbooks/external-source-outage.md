# Runbook: External source outage or contract failure

Alerts: source_stale, data_contract_failure

## Trigger

A source (`open_meteo`, `carbon_intensity`, `eia`, `fred`) is older than its limit in `configs/ops/alerts.toml` (limits mirror `configs/data/sources.toml`), or incoming rows failed their contract.

## Impact

Forecast features age; once past `stale_max_lag_days` the forecaster serves the seasonal fallback, then 503. Pricing decisions degrade to `forecast_stale` / `input_unavailable` (HOLD or UNAVAILABLE), never to a guessed price.

## Diagnose

1. `uv run python -m praxis.data freshness` shows the newest observation per source.
2. Check the source's public status page; do NOT retry in a tight loop and never load-test a public free API.
3. Contract failure: inspect the quarantined batch (provenance `quality_status`) for the failing field before re-ingesting.

## Mitigate

1. Nothing to fix on our side for an upstream outage: confirm pricing is HOLDing/UNAVAILABLE (`GET /v1/ops/health`, model section) and wait.
2. Contract change upstream: pin the old schema, open an ADR, update the normaliser, replay the raw archive (`python -m praxis.data replay`) which is offline and idempotent.

## Verify

1. `data_source_age_s` below its limit and the `source_stale` alert clears after two evaluations (incident auto-resolves).
2. A fresh pricing cycle no longer reports `forecast_stale`.

## Escalate

Page the data owner if a source is down for more than its freshness limit twice over, or if a contract failure repeats after a replay.
