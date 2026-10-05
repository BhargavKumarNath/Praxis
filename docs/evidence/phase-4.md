# Phase 4 Evidence Report

```text
Phase: 4 - Baseline Demand Forecasting
Date: 2026-10-05
Code revision: 5cf676b (HEAD, Phase 3) + uncommitted working tree (no commits made, per policy);
               reports and the artifact record code_revision "5cf676b-dirty"
Environment: Linux, Python 3.12.14, numpy 2.5.3, lightgbm 4.7.0, scipy 1.18.1, duckdb 1.5.6,
             dbt-duckdb (Phase 2), 16 cores (LightGBM num_threads = 4)
All demand data is SYNTHETIC (Phase 1 simulator). External signals used only in the ablation are
real (Open-Meteo, NESO, EIA, FRED). No cloud resource was created or used.
```

Design: `docs/forecasting.md`, ADR 0010 (method, leakage rules, pre-registered acceptance),
ADR 0011 (hybrid champion, re-registered acceptance). Raw results: `phase-4-backtest-eval-seed42.json`,
`phase-4-backtest-dev-seed1.json`, `phase-4-ablation-external-seed42.json`, `phase-4-artifact-manifest.json`.

## Protocol (in order)

1. Pre-registered (before any model ran): evaluation world `configs/simulator/scenarios/forecast_eval.toml`
   (1,000 customers, 196 days, seed 42; 7 unannounced demand spikes, 4 list-price changes, 1 capacity shock),
   hyperparameters and `[acceptance]` thresholds (`configs/forecast/demand.toml`, ADR 0010).
2. Method development on a **development world** only (same scenario, seed 1; first at 300, then at 1,000
   customers). Two findings there:
   * in-sample LightGBM quantiles under-covered (80% interval ~0.71) -> out-of-sample quantile calibration
     added (ADR 0010, `calibration_days = 28`); dev coverage then 0.494 / 0.792 / 0.900 (50/80/90%);
   * ridge beat the LightGBM point forecast (vWAPE 0.1349 vs 0.1404). Ten point variants were tried on dev
     only (listed in ADR 0011); none beat ridge. The project owner chose the **hybrid champion** (ridge mean +
     calibrated LightGBM quantiles) and the re-registered acceptance (ADR 0011) **before seed 42 was run**.
3. Dev world with the final code: all 11 re-registered checks pass (`phase-4-backtest-dev-seed1.json`).
4. Evaluation world (seed 42) run **once** with the final code: `make forecast-data forecast-backtest
   forecast-train`. No threshold, hyperparameter or code path changed afterwards. (One later fix touched only
   the `predict` CLI's error exit, see Fixes.)

## Tests executed

`make check` exit 0 (ruff, ruff format, mypy strict over 148 files, pytest with coverage against local Postgres
and the Pub/Sub emulator, `events-check` chaos smoke, schema sync, secret scan, frontend, terraform validate):
**690 passed, 1 skipped** (live BigQuery module, `make bq-verify` only) in 357 s; total coverage 99%.
`pip-audit` on the locked dependency set: no known vulnerabilities. Forecasting coverage: artifact, config,
metrics, models, panel 100%; backtest 98%, service 98%, features 97%, CLI 96%, warehouse 95%. Uncovered
lines are defensive guards (empty origin list, unknown-region context row, `__main__` entry).

Forecasting: 131 tests in `tests/forecasting/` (+3 simulator scenario-overlay tests, +3 provenance tests).

| Required (required_test.md section 10) | Test |
| --- | --- |
| Leakage: features cannot include future outcomes | `test_leakage.py::test_features_never_depend_on_outcomes_after_the_origin` (Hypothesis, 40 origins/seeds, all outcomes and context after T rewritten -> bit-identical features); negative control `test_negative_control_leaky_feature_is_detected`; planned price only on the target day; `initial_tier` not `current_tier` (`test_warehouse.py`) |
| Temporal validation, no random split | `test_every_backtest_fold_trains_strictly_before_it_evaluates`, `test_split_refuses_overlapping_train_and_test`, `test_no_random_split_exists_in_the_backtest_api` |
| Baseline comparison against pre-defined naive baseline | `evaluate_acceptance` (pre-registered thresholds, pinned by `test_preregistered_acceptance_thresholds_are_unchanged`); each check proven able to fail (`test_each_preregistered_check_can_fail`, 7 cases) |
| Segment evaluation: region, product, segment, normal vs spike | `run_backtest` slices (+ horizon); `test_report_covers_every_model_slice_and_comparison` |
| Pinball loss, interval coverage, crossing detection | `test_metrics.py` (hand-calculated), `test_rearrangement_sorts_and_counts_crossing_rows`, ordered-quantile assertions |
| Reproducibility | `test_lightgbm_training_is_bit_reproducible`, `test_same_inputs_give_the_same_model_version_regardless_of_time`; `train` CLI trains twice and refuses to save if they differ |
| Serving: model version returned | `test_service.py`, `test_api.py::test_forecast_response_contract` |
| Serving: stale feature rejection / fallback | `test_stale_features_fall_back_to_the_baseline` (lag 2, 7), `test_expired_features_are_rejected_not_forecast` (lag 8), API 503 |
| Serving: malformed input | `test_malformed_input_gets_an_explicit_422` (7 payloads), unknown series 404, bad price 400 |
| Serving: batch input | `test_batch_subset_and_horizons`, `test_batch_request_for_selected_series` |
| Serving: latency budget | `test_latency_budget_over_the_real_warehouse_path` (20 calls via DuckDB, p95 < 500 ms) |
| Inference path emits monitoring metrics | `test_metrics_and_structured_log_carry_the_model_version`, `/v1/forecasts/demand/metrics` |
| Real boundaries | `test_pipeline.py`: simulator -> DuckDB raw -> real `dbt run` -> panel (sum equals raw events exactly) -> backtest -> artifact -> service |

## Results (evaluation world, seed 42; 16 weekly origins 2026-03-29..2026-07-12, 72 series x 7 horizons = 8,064 forecasts)

| Model | vWAPE | MAE | RMSE | Pinball | Cov 50 | Cov 80 | Cov 90 | Bias |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| seasonal naive (documented baseline) | 0.1661 | 1,471.7 | 5,215 | 0.0516 | 0.489 | 0.802 | 0.909 | -2.0% |
| seasonal moving average | 0.1401 | 1,221.7 | 4,479 | 0.0422 | 0.485 | 0.800 | 0.897 | -5.1% |
| ridge | 0.1288 | 1,116.0 | 4,155 | 0.0399 | 0.489 | 0.798 | 0.902 | -1.1% |
| LightGBM (challenger: point + calibrated quantiles) | 0.1357 | 1,162.9 | 4,262 | 0.0397 | 0.492 | 0.795 | 0.898 | +0.2% |
| **hybrid (champion)** | **0.1288** | 1,116.0 | 4,155 | **0.0397** | 0.492 | 0.795 | 0.898 | -1.1% |

### Acceptance (re-registered, ADR 0011): PASS, 11 of 11

| Check | Value | Threshold |
| --- | --- | --- |
| vWAPE ratio vs seasonal naive | 0.776 | <= 0.90 |
| vWAPE below seasonal naive | -0.0373 | < 0 |
| vWAPE below seasonal moving average | -0.0113 | < 0 |
| 95% block-bootstrap CI of vWAPE(hybrid) - vWAPE(naive) | [-0.0455, -0.0292] | upper < 0 |
| pinball ratio vs seasonal naive | 0.770 | < 1.0 |
| pinball below seasonal naive / moving average / ridge | -0.0119 / -0.0025 / **-0.0002** | < 0 |
| 80% interval coverage | 0.795 | [0.74, 0.86] |
| 50% interval coverage | 0.492 | [0.43, 0.57] |
| worst region / product / segment slice vs naive | 0.812 (gpu_minutes) | <= 1.10 |
| reproducibility | identical model files and version on retrain | exact |

Bootstrap (2,000 resamples of 16 weekly origin blocks): hybrid - moving average [-0.0151, -0.0073];
hybrid - LightGBM point [-0.0095, -0.0046]. The pinball advantage over ridge's residual quantiles is real
but small (0.0397 vs 0.0399); the calibrated quantiles' value is mainly in segment-level calibration.

### Slices (hybrid vWAPE; 80% coverage)

| Slice | Hybrid | Naive | LightGBM pt | Cov 80 |
| --- | --- | --- | --- | --- |
| eu_west / us_east / us_west | 0.089 / 0.141 / 0.143 | 0.117 / 0.183 / 0.179 | 0.098 / 0.144 / 0.151 | 0.815 / 0.813 / 0.804 |
| eu_central / ap_southeast (smallest regions) | 0.169 / 0.174 | 0.210 / 0.226 | 0.177 / 0.179 | 0.761 / 0.777 |
| api / cpu / data / gpu / premium | 0.121 / 0.111 / 0.123 / 0.148 / 0.155 | 0.155 / 0.147 / 0.162 / 0.182 / 0.196 | | 0.785 / 0.842 / 0.812 / 0.775 / 0.755 |
| starter / growth / enterprise | 0.127 / 0.101 / 0.136 | 0.160 / 0.129 / 0.175 | 0.130 / 0.105 / 0.144 | 0.801 / 0.839 / **0.745** |
| horizon 1 / 4 / 7 | 0.121 / 0.116 / 0.129 | 0.164 / 0.153 / 0.162 | | 0.832 / 0.790 / 0.783 |
| normal days (7,902 rows) | 0.121 | 0.160 | 0.129 | 0.805 |
| **spike days (162 rows)** | **0.398** | **0.394** | **0.369** | **0.272** |

### External-signal ablation (`--external`, same world plus 146K real signal records)

| Model | vWAPE base / +external | Pinball base / +external |
| --- | --- | --- |
| ridge | 0.1288 / 0.1304 | 0.0399 / 0.0401 |
| LightGBM | 0.1357 / 0.1364 | 0.0397 / 0.0395 |
| hybrid | 0.1288 / 0.1304 | 0.0397 / 0.0395 |

No lift (point slightly worse). This is the expected result: simulated demand does not depend on weather,
carbon, grid demand or CPI, so lift would itself have been a warning sign. External features stay off.

## Performance metrics

* Evaluation world build (simulate 609,367 events in 15.5 s, 145 MB peak; load; `dbt run`): ~25 s.
* Backtest: 340 s for 16 origins (LightGBM 8 boosters + 7 calibration boosters per origin; ridge 0.5 s total).
* Training: 83,160 rows, two identical trainings, artifact `demand-hybrid-19a1992e2e43` (7 quantile models +
  ridge coefficients, 15 KB manifest).
* Serving, real artifact over the 145 MB evaluation warehouse, all 72 series x 7 horizons (504 points),
  single cold call: fresh (model) 171 ms, stale (fallback) 122 ms; lag 11 days -> `features_unavailable`,
  exit 2. Test-suite latency check: p95 of 20 calls < 500 ms budget.
* Raw quantile crossing rate (before rearrangement): 7.8% of rows; 0 after.

## Statistical metrics

See Results. Calibration offsets of the served model (scaled units, q0.05..q0.95):
-0.042, -0.038, -0.024, -0.012, +0.021, +0.047, +0.087 (intervals widened, as diagnosed on dev).

## Failures found

* Dev world: in-sample quantile under-coverage (80% interval ~0.71). Fixed by out-of-sample calibration.
* Dev world: LightGBM point forecast lost to ridge; resolved by the owner's decision (ADR 0011), not by tuning.
* Config edit accident: a scripted replacement of `[acceptance]` matched the header comment and deleted the
  rest of `configs/forecast/demand.toml`; detected immediately (config failed to load) and rewritten with the
  identical values plus the ADR 0011 acceptance table, before any evaluation run.
* `predict` CLI crashed with a traceback when features were too old; now exits 2 with a JSON error (test added).
  Found after the evaluation run; touches only the CLI error path, not model, features or backtest.
* Ridge baseline emitted NumPy warnings on an all-missing feature column; moments now computed explicitly.

## Known limitations

* **Spike days are not forecastable** and intervals collapse there (80% coverage 0.27 on 162 rows). The hybrid is
  no better than naive on spike days; LightGBM point is best (0.369). Spike detection / regime handling is open.
* Enterprise segment 80% coverage 0.745 (inside the band, at its edge); small regions (eu_central, ap_southeast)
  under-cover slightly (0.76-0.78).
* The ridge price coefficient is predictive, not causal. Elasticity is Phase 5.
* Demand is censored when a product is unavailable; the evaluation world has no outages.
* Daily grain; features are refreshed when a day completes, not every 5 minutes.
* Model directory is local; MLflow registry, champion pointer and GCS artifacts are Phase 10. OpenTelemetry export
  of `ForecastMetrics` is Phase 11.
* One evaluation world (seed 42, 1K customers). The dev world agreed closely (vWAPE ratio 0.775 vs 0.776), but
  results at 10K+ customers are not measured.
* **Cross-rebuild reproducibility is not exact** (found 2026-10-05 by the first local `nightly-science` run).
  Rebuilding a world from its seed gives bit-identical demand, served and active counts, but the
  `feat_region_daily` view averages floats in physical row order, which differs between DuckDB builds: three
  service-context columns differ by ~1e-16. That changes the panel `data_version` and moves LightGBM metrics at
  the 1e-4 level (dev world: vWAPE 0.1404 -> 0.1405, coverage 50 0.494 -> 0.496; acceptance unaffected).
  Retraining on the *same* warehouse is bit-identical (what the reproducibility checks above test). Fix pending:
  order-independent aggregation of context features (e.g. exact `math.fsum` over ordered hourly rows, or
  DECIMAL averaging in the view).
* `load_price_plan` reads exposures bounded above only (needs full history for the first price); fine on DuckDB,
  needs a price dimension before running on BigQuery with partition filters.

## Cloud cost incurred

GBP 0. Local DuckDB and Docker only. ~70 small requests to free public APIs for the ablation (archived).

## Gate

PASS

## Reason

| Gate (project_plan.md Phase 4) | Evidence |
| --- | --- |
| Model beats documented naive baseline on pre-defined metrics | hybrid vWAPE 0.776 x seasonal naive, bootstrap CI entirely below 0; pinball 0.770 x; all 11 re-registered checks pass on the held-out world |
| No target leakage | perturbation property test + negative control; temporal fold assertions; static segment; planned price whitelisted and tested |
| Prediction intervals tested for coverage | 50% 0.492, 80% 0.795, 90% 0.898 pooled; per-slice coverage reported, spike-day failure disclosed |
| Stale-feature behaviour defined | ADR 0010 policy (fresh model / stale fallback / reject), tested in service, API and CLI |
| Model artifact reproducible | content-derived version; retrain on the same warehouse gives identical files; checksums verified on load; tamper tests. Not exact across warehouse rebuilds (see Known limitations) |
| Inference path emits monitoring metrics | `ForecastMetrics` counters + latency, structured `forecast.*` logs with model version, `/metrics` endpoint |
