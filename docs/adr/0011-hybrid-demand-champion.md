# ADR 0011: Hybrid demand champion (ridge mean + calibrated LightGBM quantiles)

Status: Accepted (Phase 4), amends ADR 0010. Decided by the project owner on 2026-10-04,
**before the evaluation world (seed 42) was run**.

## Context
ADR 0010 pre-registered LightGBM (point + quantiles) as the candidate and required it to
beat every baseline. On the development world (same scenario, seed 1, 1,000 customers,
16 weekly origins) the full rolling-origin backtest gave:

| Model | vWAPE | Pinball | Coverage 50 / 80 |
| --- | --- | --- | --- |
| seasonal naive | 0.1742 | 0.0549 | 0.487 / 0.789 |
| seasonal moving average | 0.1456 | 0.0448 | 0.493 / 0.783 |
| ridge | **0.1349** | 0.0418 | 0.478 / 0.775 |
| LightGBM (calibrated) | 0.1404 | **0.0414** | 0.494 / 0.792 |

Ten point-model variants were tried on the development world only (fewer leaves, larger
leaves, fewer rounds, uniform weights, seasonal-average and ridge initial scores, linear
trees, an equal ridge/LightGBM combination). None beat ridge on the mean (best
0.1348 vs 0.1349). The simulated demand is close to linear in the scaled seasonal lags, and
tree corrections fit noise. LightGBM does help where the relationship is not linear:
spike days (0.360 vs 0.382) and the starter segment. Its calibrated quantiles were the best
probabilistic forecast. Searching further would have been tuning by forking paths.

## Decision
* **Champion = hybrid.** Point forecast (expected demand) from the ridge model; quantiles
  from the calibrated LightGBM quantile models (ADR 0010). Each component does the job it
  was measured best at. The mean can occasionally fall outside a central interval; they
  are different estimands and both are reported.
* **LightGBM point stays a challenger.** It is trained and reported in every backtest
  (all metrics and slices) but not served. Phase 10's shadow/challenger machinery can
  revisit it on richer worlds.
* **Re-registered acceptance** (`[acceptance]` in `configs/forecast/demand.toml`), applied
  to the hybrid:
  * vWAPE <= 0.90 x seasonal naive (unchanged);
  * point vWAPE strictly below seasonal naive and seasonal moving average. Ridge is not
    in this list because it *is* the hybrid's point model; the LightGBM point challenger
    is reported, not gated;
  * 95% block-bootstrap CI of vWAPE(hybrid) - vWAPE(naive) entirely below zero (unchanged);
  * pinball loss strictly below **every** baseline, ridge's empirical quantiles included
    (stricter than ADR 0010, which compared with seasonal naive only);
  * coverage 80% in [0.74, 0.86], 50% in [0.43, 0.57] (unchanged);
  * no region / product / segment slice more than 10% worse than seasonal naive (unchanged);
  * retraining reproduces the artifact exactly (unchanged).
* **Artifact.** Ridge coefficients (`ridge.json`) and one LightGBM text model per quantile,
  with the calibration offsets, all checksummed in the manifest. Model versions are
  prefixed `demand-hybrid-`. The stale-feature fallback stays the seasonal moving average.

## Consequences
* The plan's "LightGBM point forecast" exists, is evaluated on every run, and is honestly
  reported as not beating a linear model on this world.
* If a later world (richer non-linear demand, interventions in Phase 5) makes LightGBM
  win, switching the point model is a champion/challenger promotion (Phase 10), not an
  edit.
* The evaluation world's results are reported exactly as they come, pass or fail.
