# Demand forecasting (Phase 4)

**All data is SYNTHETIC** (Phase 1 simulator). Decisions: ADR 0010 (method, leakage rules,
pre-registered acceptance), ADR 0011 (hybrid champion). Evidence: `docs/evidence/phase-4.md`.

```text
dbt marts (fct_usage_daily, dim_customer, feat_region_daily, fct_price_exposures,
           fct_service_metrics_hourly)
   |  praxis.forecasting.warehouse: dense panel (72 series x days) + price plan
   v
features.build_features(panel, origin, horizons, plan)   <- outcomes <= origin only
   |
   +--> baselines: seasonal naive, seasonal moving average, ridge   (residual quantiles)
   +--> LightGBM: point (challenger) + 7 calibrated quantile models
   |
   +--> backtest: expanding window, weekly origins, slices, block bootstrap, acceptance
   +--> artifact: hybrid champion (ridge mean + LightGBM quantiles), checksummed lineage
   +--> service / API: freshness policy, stale fallback, metrics, /v1/forecasts/demand
```

## What is forecast

| Item | Choice |
| --- | --- |
| Series | region x product x segment; segment = tier at signup (static) |
| Target | requested units per UTC day = served + throttled (unconstrained demand) |
| Horizons | 1..7 days after the feature date |
| Outputs | expected demand (point) and quantiles 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95 |
| Scale | each series divided by its own 28-day mean at the origin; one pooled model |

Features (`demand_features.v1`): same-weekday lags (`y[D-7k]`, k = 1..4) and their mean,
last value, 7-day mean, exponentially weighted mean, 28-day std, log scale, served ratio,
active-customer ratio, regional service context at the origin (utilisation, latency, errors),
planned-price changes (target vs origin, last week, vs first price), day of week, horizon, and
region / product / segment codes. Optional `ext_*` features (real weather, carbon, grid demand,
CPI at the origin) exist for the ablation and are off by default.

## Leakage rules (tested)

* Every outcome-derived feature is computed from `panel.history(origin)`.
  `tests/forecasting/test_leakage.py` rewrites everything after the origin (Hypothesis, 40
  random origins and seeds) and requires bit-identical features; a deliberately leaky feature
  makes the same check fail.
* The planned price is the only future input, and only its value on the target day matters.
* Backtest folds assert `max(train target day) <= origin < min(test target day)`.
* `initial_tier`, never `current_tier`; FRED values respect the 45-day release lag (Phase 2).

## Run it

```bash
make forecast-data                       # evaluation world: scenario + seed 42, 1K customers (~25 s)
make forecast-backtest                   # rolling-origin backtest + acceptance (exit 1 on FAIL)
make forecast-train                      # train twice (must be identical), save artifact
make forecast-data FC_SEED=1             # development world for method work (never seed 42)
make forecast-signals                    # ablation input: ingest real signals for the world's dates

.venv/bin/python -m praxis.forecasting --db DB backtest [--external] [--scenario S] [--report R]
.venv/bin/python -m praxis.forecasting --db DB train --backtest-report R [--out DIR]
.venv/bin/python -m praxis.forecasting --db DB predict --model DIR [--as-of DATE]
```

`train` refuses to save unless the supplied backtest report passed acceptance on the same
config hash and data version, and unless a second training run reproduces the model
exactly. `--allow-unvalidated` exists for development only and is recorded in the manifest
(`backtest: null`).

## Serving

`PRAXIS_FORECAST_MODEL_DIR=<artifact dir>` and `PRAXIS_WAREHOUSE_PATH=<duckdb>`, then `make run`.

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/forecasts/demand` | `{series?, horizons?, planned_prices_micros?}` -> forecasts |
| `GET /v1/forecasts/demand/model` | model version, champion, data / feature version, code revision |
| `GET /v1/forecasts/demand/metrics` | request / outcome / source / freshness counters, latency |

Every response carries `model_version`, `forecast_created_at`, `feature_date`,
`feature_timestamp` (all data used is strictly before it), `feature_lag_days`,
`freshness_status`, `source` and `is_synthetic: true`.

| Feature lag (today UTC - feature date) | Behaviour |
| --- | --- |
| <= 1 day | `fresh`, hybrid model |
| 2..7 days | `stale`, seasonal moving average, `fallback_reason: stale_features` |
| > 7 days, or no complete day | `503 features_unavailable` |

A *complete* day has all 24 hourly service rows for every region. Errors are explicit:
`422 invalid_request`, `404 unknown_series`, `400 planned_price_out_of_range` /
`unknown_product` / `invalid_horizon` / `too_many_series`, `503 forecast_unavailable`
(no model loaded) / `warehouse_unavailable`. Planned prices must lie within x1.5 of the
current list price (the model has no evidence outside it). Each call is logged as
`forecast.served` / `forecast.unavailable` / `forecast.rejected_request` with the model
version and latency, and counted in `ForecastMetrics` (OpenTelemetry export is Phase 11).

## Limitations

* The forecaster's price features are predictive, not causal; elasticity is Phase 5.
* Unannounced spikes cannot be predicted before they start (reported as a separate slice).
* Demand is censored when a product is unavailable (no usage event when nothing is served).
* Daily grain only; the simulator has no intra-day customer demand.
* The model directory is local; MLflow registry, champion pointer and GCS artifacts are Phase 10.
