# Price elasticity and causal price science (Phase 5)

**All data is SYNTHETIC** (Phase 1 simulator). Decision: ADR 0012 (identification, estimand,
estimators, gates, the pre-evaluation amendment). Evidence: `docs/evidence/phase-5.md`.

```text
configs/experiments/elasticity_eval.toml        business-side record of each randomised price test
configs/simulator/scenarios/elasticity_eval.toml the world that executes them (agreement is tested)
   |  simulate -> raw load -> dbt marts (dim_customer, fct_usage_daily, fct_price_exposures)
   v
praxis.elasticity.warehouse   read marts only (never the simulator)
praxis.elasticity.dataset     eligible units, recomputed arm vs logged exposure, outcome window
praxis.elasticity.validity    SRM, assignment, exposure, contamination, balance, missing outcomes  (gates)
                              late-arrival SRM, factorial interference, guardrails               (reported)
praxis.elasticity.estimators  log-log FE slopes (pooled / tier / industry / cell / dose / per test),
                              cluster-robust SEs, IV, naive pre/post (failure case), empirical Bayes
praxis.elasticity.bayes       two-stage hierarchical model (PyMC) + diagnostics gates
praxis.elasticity.artifact    gated, checksummed estimates for the Phase 6 optimiser
   |
praxis.science                ground truth joined ONLY here; pre-registered acceptance
```

## Prediction is not causation

A regression of demand on the prices the business happened to charge does not estimate how
demand responds to price: prices change at chosen moments, alongside trends, seasonality and
whatever prompted the change. Praxis identifies elasticity from **randomised price tests**.
Each customer is assigned to an arm by hashing `salt:customer_id`
(`praxis.domain.experiments.assign_arm`), and treated customers pay `ratio x` the list price.
Randomisation makes the arm independent of everything else, so the arm difference in demand
growth is the causal effect. As a reminder of what a control group buys, the report includes a
**naive before/after** on treated customers only. On the evaluation world it is off by 0.24 to
0.33 per test, in a direction set by the common demand trend.

## Design of the evaluation world

| Item | Value |
| --- | --- |
| Customers | 8,000 (`EL_CUSTOMERS`), seed 42 held out, seed 1 for development |
| Pre-period | days 0-27 (four whole weeks), no tests |
| Tests | five concurrent, one per product, days 28-55, 50/50, independent salts (2^5 factorial) |
| Price ratios | +20% (api_requests, gpu_minutes, premium_latency), x 1/1.2 (cpu_minutes, data_transfer_gb) |
| Assignment unit | customer; one arm per customer per test |
| Control price | list price on the assignment date; treatment = round(list x ratio) +-1 micro |

## Units, eligibility and outcome

* Unit = (test, customer). **Eligible** on pre-treatment information only: created before the
  pre-period, not churned before the assignment date, >= 56 requested units of the product
  in the pre-period.
* Customers arriving after assignment are **never eligible**. Price changes conversion, so
  late arrivals are selected by their arm. Their arm split is reported as `late_arrival_srm`.
* Outcome `delta = log((Y_post + 0.5)/d_post) - log((Y_pre + 0.5)/28)`, where Y = requested
  units (served + throttled) and d_post = active days (churn ends the window). An outcome is
  missing when d_post < 14.
* Arms are **recomputed** from the registry salt and compared with the logged exposure on the
  assignment date. Each unit gets one status: `ok`, `missing`, `arm_mismatch`, `contaminated`
  (charged the other arm's price), `price_error` or `conflicting`.

## Estimand

The fixed-effect OLS slope over a set of units S estimates

```text
sum_u w_u e_u / sum_u w_u,   w_u = pi (1 - pi) (log ratio_u)^2
```

where e_u is the customer's own-price elasticity of requested units. Every test here has the
same |log ratio| and pi, so this is the plain mean over the analysed units. `praxis.science`
computes ground truth with exactly these weights over exactly these units (`units.json`).
Segments use the tier **at signup** (`initial_tier`), which is pre-treatment.

## Estimators

| Step | Model | Role |
| --- | --- | --- |
| 1 | log-log OLS, test fixed effects, CR1 cluster-robust by customer, t(G-1) | pooled baseline (gated) |
| 2 | same with one slope per tier / industry / tier x industry cell (>= 50 units) | segment interactions |
| 2b | empirical Bayes shrinkage of cells toward tier + industry (Paule-Mandel) | closed-form cross-check |
| 3 | two-stage hierarchical Bayesian model (PyMC) over the 18 cells | segment elasticities (artifact) |
| - | IV: assigned ratio instruments the charged price | contamination / non-compliance |
| - | naive pre/post, treated only | failure case, never used |

Hierarchical model:
`b_c ~ N(theta_c, se_c)`, `theta_c ~ N(mu + a_tier + b_industry, sigma_cell)`, with
`a_tier ~ ZeroSumNormal(1)`, `b_industry ~ ZeroSumNormal(1)` (fixed scales) and
`sigma_cell ~ HalfNormal(0.5)`. Cells borrow strength from their tier and industry, by an
amount (`sigma_cell`) learned from the data. theta is integrated out for sampling and drawn
from its exact conditional afterwards. Sampling uses NUTS with 4 chains x 2,000 draws,
target_accept 0.99, and a fixed seed. Tier, industry and pooled summaries are design-weighted
averages of the cell draws, so they target the same estimand as the OLS slopes.

## Gates

Truth-free (in the analysis; no artifact unless all pass), `configs/elasticity/elasticity.toml`:

| Gate | Rule |
| --- | --- |
| SRM | chi-square of eligible units vs designed split, p >= 0.001 |
| Assignment | logged arm == recomputed arm for every eligible unit |
| Exposure logging | every eligible unit has exactly one exposure on the assignment date |
| Contamination / price errors | none (clean design) |
| Balance | omnibus difference-in-means chi-square over pre-period log demand, tenure flag, tier, industry, region; p >= 0.001 (|SMD| > 0.1 is reported, not gated) |
| Missing outcomes | <= 10% per arm, |difference| <= 2 pp |
| Bayesian | R-hat <= 1.01; bulk and tail ESS >= 400; 0 divergences; posterior predictive p in [0.025, 0.975] for chi-square discrepancy, between-cell SD, min, max; prior sensitivity <= 0.5 posterior SD for pooled and tier elasticities |

Ground truth (`praxis.science`, `configs/elasticity/acceptance.toml`): sign, pooled magnitude
(<= 10% and |z| <= 3), tier magnitude (<= 25%), ordering of every identifiable segment pair,
>= 70% of cells covered by their 90% intervals, pooling must not raise cell RMSE. A
contamination world additionally needs detection within 3 pp, IV within 15%, and |ITT| < |IV|.

## Running

```bash
make elasticity-data EL_SEED=1          # dev world: simulate -> load -> dbt (~50 s)
make elasticity-analyze EL_SEED=1       # validity + estimators + PyMC (+ artifact if gates pass) (~35 s)
make elasticity-evaluate EL_SEED=1      # pre-registered ground-truth acceptance
make elasticity-contamination EL_SEED=1 # contamination world: detection, ITT dilution, IV
make nightly-science                     # all of the above on seed 1 (never the held-out seed 42)
```

Outputs: `data/elasticity/<name>-seed<S>-c<N>/analysis/{report,units,recovery}.json` and
`data/models/elasticity/elasticity-hier-<hash>/{manifest,estimates}.json`.

## Known failure cases and limits

* **No control group.** A naive before/after absorbs the common trend: -0.97 vs -1.24 on
  api_requests, -1.67 vs -1.38 on cpu_minutes (seed 42). This is why the tests are randomised.
* **Contamination dilutes ITT.** With 20% of control customers charged the treatment price,
  ITT shrinks by about 20% (-1.01 vs IV -1.26 on api_requests). The validity gate refuses the
  analysis; IV recovers the pooled truth (0.5% error), but the artifact is not published.
* **Small counts bias log outcomes.** E[log Y] < log E[Y] by an amount that depends on the arm,
  which is why low-volume units (< 2 units/day) are excluded before assignment is looked at.
* **Between-group variances are barely identified** with three tiers and six industries. The
  learned-scale model funnelled (divergences) in the development and synthetic worlds, which
  is why main-effect scales are fixed (ADR 0012, Amendment).
* **Composition, not dose.** Raises and cuts were tested on different products, hence on
  different customers, so "raise" (-1.24) and "cut" (-1.42) differ because their truths differ
  (-1.21, -1.43). This is not evidence of non-constant elasticity.
* **Interference.** In this world, tests interact only through churn and shared capacity. The
  factorial check regresses each test's outcome on the customer's other assignments, centred on
  their design means. The uncentred version flagged false interference because product mix
  correlates with elasticity (fixed; regression test).
* **Two-stage approximation.** Cell SEs are treated as known (cells average ~900 units).
* **Tested range only.** Elasticity is identified for |log ratio| <= 0.18. The simulator's
  elasticity is constant by construction, so a real world's curvature is not tested here.
* **Intensive margin.** The outcome is demand per active day. Price effects on churn and
  conversion are reported as guardrails, not folded into the elasticity.
