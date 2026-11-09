# Demand forecasting

The forecaster predicts daily demand one to seven days ahead, with calibrated intervals, for
every region x product x segment series (72 in the default world). It feeds pricing and drift
monitoring. Decisions: ADR 0010 (method, leakage rules, acceptance) and ADR 0011 (the hybrid
champion). Results: [results.md](results.md#forecasting).

## What is forecast

| Item | Choice |
| --- | --- |
| Target | Requested units per UTC day, served plus throttled, so demand is not capped by capacity |
| Segment | The customer's tier at signup, never the current tier (that would leak later changes) |
| Horizons | 1 to 7 days after the feature date |
| Outputs | Expected demand and quantiles 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95 |
| Model | Ridge for the mean, LightGBM for the quantiles, calibrated out of sample on 28 days and rearranged so they never cross |

Features (`demand_features.v1`) are same-weekday lags, rolling means and spread, regional
service context at the origin, planned-price changes, calendar, horizon and series codes.
Optional external features (weather, carbon, grid demand, CPI) exist for an ablation and are off
by default, because they gave no lift.

## Leakage rules

- Every outcome-derived feature comes from data up to the origin. A Hypothesis test rewrites
  everything after the origin at 40 random origins and requires bit-identical features; a
  deliberately leaky feature makes the same test fail.
- The planned price on the target day is the only input about the future.
- Backtest folds assert that every training target precedes the origin.
- FRED values respect their 45-day publication lag.

Validation is a rolling-origin backtest with weekly origins and a block bootstrap. There is no
random split anywhere.

## Commands

```bash
make forecast-data FC_SEED=1        # development world (the default seed 42 is held out)
make forecast-backtest FC_SEED=1    # backtest and pre-registered acceptance; exit 1 on FAIL
make forecast-train FC_SEED=1       # trains twice, refuses to save unless both runs match
python -m praxis.forecasting --db DB predict --model DIR [--as-of DATE]
```

`train` refuses to save unless the backtest report it is given passed acceptance on the same
config hash and data version.

## Serving

Set `PRAXIS_FORECAST_MODEL_DIR` and `PRAXIS_WAREHOUSE_PATH`, or
`PRAXIS_FORECAST_FROM_REGISTRY=true` with a database to serve the registry's champion, then
`make run`.

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/forecasts/demand` | Forecasts for some or all series, optionally at planned prices |
| `GET /v1/forecasts/demand/model` | Model version, stage, data and feature version, code revision |
| `GET /v1/forecasts/demand/metrics` | Request, outcome and freshness counters, latency |

Every response names the model version and the feature date, and says whether the features
were fresh.

| Feature age | Behaviour |
| --- | --- |
| up to 1 day | `fresh`: the hybrid model |
| 2 to 7 days | `stale`: a seasonal moving average, labelled as a fallback |
| older, or no complete day | `503 features_unavailable` |

Pricing only moves a price on a fresh forecast. Planned prices must lie within 1.5x of the
current list price, because the model has no evidence beyond that. Errors are explicit
(`422`, `404 unknown_series`, `400` for an out-of-range price or horizon, `503` when no model
or warehouse is available).

## A defect worth knowing about

Before Phase 10, loading an artifact rebuilt the ridge's one-hot blocks in sorted key order
instead of fit order, so every loaded model applied its categorical coefficients to the wrong
columns. Training and the backtest were unaffected, but the API and the Phase 6 and 9 pricing
runs loaded artifacts. It was fixed, a regression test now compares predictions, and every
candidate is checked for `serving_parity`. The affected pricing results stand as measured.

## Limits

- The price features are predictive, not causal. The causal response comes from
  [elasticity.md](elasticity.md).
- Unannounced spikes cannot be forecast before they start.
- Demand is censored when a product is unavailable.
