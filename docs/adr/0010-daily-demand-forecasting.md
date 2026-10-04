# ADR 0010: Daily probabilistic demand forecasting with pre-registered acceptance

Status: Accepted (Phase 4). The champion model and acceptance checks are amended by ADR 0011.

## Context
Pricing (Phase 6) needs expected demand and its uncertainty per region and product, a few
days ahead. The simulator steps customers daily (ADR 0007), so there is no intra-day
customer demand to forecast; hourly data exists only for regional service metrics. The
model must beat a documented naive baseline on rolling time-series splits, must not see
the future, and must say how uncertain it is. Everything here is SYNTHETIC.

## Decision
* **Grain and target.** One series per region x product x segment, where segment is the
  customer's tier *at signup* (static, so it cannot leak a later tier change). Target =
  requested units per UTC day (`units + throttled_units`): unconstrained demand, which is
  what a price decision changes. Horizons 1 to 7 days after the feature date. 72 series in
  the default world (GPU is not sold in `ap_southeast`).
* **What a feature may see.** A forecast at origin T for day D = T + h uses outcomes dated
  <= T only. Two inputs about D are allowed because the business controls or knows them
  in advance: the calendar (day of week) and the *planned* list price (`PricePlan`). The
  feature builder slices history to <= T before computing anything, and a perturbation test
  shows that rewriting every outcome after T leaves the features unchanged. A
  deliberately leaky feature makes the same test fail (negative control).
  Not used: hour (no intra-day customer dynamics), holidays (the simulated world has
  none), `current_tier` (it encodes future tier changes).
* **Pooled models on scaled targets.** Every series is divided by its own 28-day mean at
  the origin, so one model serves all 72 series. Training rows are weighted by value
  (scale x a fixed GBP value per unit), which matches the evaluation metric.
* **Baselines first, all probabilistic.** Seasonal naive (`y[D-7]`, the documented naive
  baseline), seasonal moving average (mean of the last four same weekdays) and ridge
  regression on the same features. Each gets quantiles from its own empirical residuals
  on the training window, so pinball loss and coverage compare like with like.
* **LightGBM.** One L2 model for the point forecast (expected demand, which an
  expected-profit optimiser needs) and one quantile model per level
  (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95). Raw quantile crossings are counted and
  reported, then removed by sorting (monotone rearrangement). Hyperparameters are fixed a
  priori (`configs/forecast/demand.toml`); nothing is tuned on the evaluation window.
  `deterministic=true`, fixed seed and thread count make retraining bit-reproducible.
* **Out-of-sample quantile calibration.** Quantile models fitted in-sample are too narrow:
  on the development world the 80% interval covered about 0.71. Each fit therefore holds
  out the last 28 days of training targets, fits the quantile models on the rest, and
  measures one additive offset per level (scaled units) so that level is hit on the
  held-out forecasts; the final models are refit on all rows and the offsets applied
  (split-conformal style). Training cost roughly doubles.
* **Development vs evaluation world.** Code and method were debugged on a development
  world (same scenario, seed 1). The evaluation world (seed 42) was run only once the code
  was final. The calibration step is the only method change made after seeing development
  results; no threshold changed.
* **Validation.** Expanding-window rolling origin only: weekly origins after 12 weeks of
  history, training rows must have target dates <= the origin. There is no random split.
  Metrics: value-weighted absolute percentage error (vWAPE, primary), MAE, RMSE,
  value-weighted pinball loss, 50% / 80% / 90% interval coverage, crossing rate. Every
  metric is also sliced by region, product, segment, horizon and spike vs normal day
  (spike days come from the scenario file and are used for evaluation only).
  Uncertainty in the headline comparison comes from a block bootstrap over weekly origins.
* **Pre-registered acceptance** (`[acceptance]` in the config, written before the first
  backtest): vWAPE <= 0.90 x seasonal naive; strictly better than every baseline; 95%
  bootstrap CI of the vWAPE difference below zero; lower pinball loss than seasonal naive;
  80% interval coverage in [0.74, 0.86] and 50% in [0.43, 0.57]; no region, product or
  segment slice more than 10% worse than seasonal naive; retraining reproduces metrics
  within 1e-9.
* **Evaluation world.** `configs/simulator/scenarios/forecast_eval.toml` (1,000 customers,
  196 days, seed 42): seven unannounced demand spikes, four scheduled list-price changes and
  a capacity shock, fixed before any model run.
* **Artifact.** A directory with one LightGBM text model per output, the fallback
  baseline's residual quantiles and `manifest.json` (model version, data version = hash of
  the training panel, feature version, code revision, parameters, backtest metrics,
  per-file SHA-256, creation time). The model version is derived from content, not the
  timestamp, so the same inputs give the same version. Loading verifies every checksum and
  refuses a feature-version mismatch. MLflow arrives with Phase 10.
* **Serving and stale features.** `ForecastService` reads the latest *complete* feature
  day from the warehouse (a day with all 24 hourly service rows per region). Lag = today
  (UTC) minus feature date:
  * lag <= 1 day: `fresh`, served by the model;
  * 1 < lag <= 7: `stale`, served by the seasonal moving-average fallback, flagged in the
    response and counted in metrics (a stale model input is worse than a transparent
    baseline whose behaviour is known);
  * lag > 7 or no data: rejected with `503 features_unavailable`; no forecast at all.
  Planned prices outside `[1/1.5, 1.5]` x the current list price are rejected: the
  model has no evidence there. Every call records latency, source, freshness and model
  version in metrics and a structured log line.

## Consequences
* The price coefficient learned by the forecaster is predictive, not causal. Causal price
  response is Phase 5's job (controlled experiments, ground-truth recovery).
* Unannounced spikes cannot be forecast before they start; spike-day error is reported
  separately and not hidden in the pooled number.
* Demand is censored where a product is unavailable (no `usage.observed` when nothing is
  served). The evaluation world has no product outages; this is a known limitation.
* External signals stay out of the default feature set unless the ablation shows lift;
  the simulated demand does not depend on them, so lift would itself be a warning sign.
* Daily grain means forecasts refresh when a day completes, not every 5 minutes. A
  sub-daily refresh is only meaningful once the simulator has sub-daily demand.
