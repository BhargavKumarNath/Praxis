# Phase 6 Evidence Report

```text
Phase: 6 - Constrained Pricing Optimiser
Date: 2026-10-06 (first held-out run, seed 42) and 2026-10-07 (re-evaluation, seed 43)
Code revision: 0cb9304 + working tree (reports record "0cb9304-dirty"); no commits made by Claude
Environment: Linux, Python 3.12, numpy 2.5.3, duckdb 1.5.6, dbt-duckdb, Postgres 17 (local
             Docker), Phase 4 forecast artifacts, Phase 5 elasticity artifacts
All customers, prices, demand, churn and results are SYNTHETIC (Phase 1 simulator). No cloud
resource was created or used. No price was executed outside tests.
```

Design: `docs/pricing.md`, ADR 0013 (incl. both amendments). Raw results:
`phase-6-eval-seed43-shadow.json` (held-out re-evaluation, decides the gate),
`phase-6-eval-seed42-shadow.json` (first held-out run, FAIL), `phase-6-dev-seed1-shadow.json`
(development, final method), `phase-6-seed43-elasticity-recovery.json` (Phase 5 recovery on the
seed-43 experiment world).

## Gate

**PASS**, on the second held-out evaluation (seed 43). The first held-out evaluation (seed 42)
**FAILED** (`no_harm`, `direction`: 13 of 17 changes lost value); the cause was diagnosed, the
method amended on the development world only (ADR 0013, second amendment), and a fresh seed
pre-registered before it was built. Read the caveat in "Reason": the seed-43 pass shows the
optimiser does no harm and is honest about uncertainty; it does NOT show it can find profitable
moves, because the churn evidence is too weak for any move to be confidently profitable.

| project_plan Phase 6 gate | Result |
| --- | --- |
| optimiser matches brute-force results on small test cases | PASS (closed-form Lerner cases, 12 random problems vs exhaustive tick enumeration) |
| property tests confirm constraints can never be violated | PASS (Hypothesis, 4 properties x 150 cases, independent scalar oracle) |
| no decision executes without audit record | PASS (store + Postgres trigger tests; 0 executions in shadow) |
| infeasible problems fail safely | PASS (INFEASIBLE, no price; unit + stress tests) |
| uncertainty can freeze pricing | PASS (unit tests; low-confidence freezes on seed 42; inflated-uncertainty stress case) |
| shadow-mode evaluation complete | DONE: seed 42 FAILED; seed 43 (pre-registered re-evaluation) PASSED |

## Protocol (in order)

1. **Pre-registered** before the optimiser ran on any world: the policy
   (`configs/pricing/policy.toml`), the shadow world (`configs/simulator/scenarios/
   pricing_shadow.toml`, the Phase 4 forecast world continued for 8 weekly cycles) and the shadow
   acceptance (`configs/pricing/shadow_acceptance.toml`). All three hash-pinned.
2. **Development world (seed 1)**:
   * the shadow evaluation first refused to run: the only seed-1 forecast artifact had been
     trained on a pre-2026-10-05 build of the dev world (lineage guard working). Retrained
     through the normal backtest-then-train gate (`demand-hybrid-24c8b0b1de4d`; backtest
     acceptance passed, vWAPE ratio 0.775);
   * **registered method**: all safety checks passed, but all 24 changes (+4.5% raises on cpu,
     gpu, data transfer) lost value in truth (-GBP 5,652 per day in total) while predicted
     contribution was accurate (ratio 0.98). Cause: per-product churn slopes from single tests
     are noise-dominated (seed 1: +0.057 / +0.006 / -0.053 / -0.012 / +0.034, SE 0.03-0.05);
     the optimiser raised exactly the products whose noise looked churn-free (winner's curse);
   * **method amendment** (ADR 0013; no policy threshold changed): partial pooling of churn slopes
     (empirical Bayes, Paule-Mandel, Morris SD) and the slope's posterior uncertainty inside
     SD / P(improvement). Result on dev: 0 changes (23 hold, 17 frozen), no value lost, one real
     opportunity missed (premium, true +GBP 47 per day);
   * the registered acceptance presupposed movement (min change share, min captured share) and
     failed for the opposite reason. **Acceptance amendment, approved by the owner** before seed
     42: gated = safety + no harm + direction of changes + calibration (>= 5 changes); change
     share and captured share reported only. Re-pinned;
   * found and fixed while checking reproducibility: the CLV proxy summed floats in DuckDB, so two
     identical runs produced different decision ids. Exact integer aggregation + regression test
     (negative control: the old SQL returns 2^53 or 2^53 + 2 depending on row order). After the
     fix three dev runs gave byte-identical `decisions.jsonl` and `shadow.json`.
3. **Held-out world (seed 42)**, run once with the final method: `make pricing-data
   pricing-shadow`. The forecast prefix matched the Phase 4 evaluation artifact exactly
   (`panel-8b9cab7bf9c927c0`, `demand-hybrid-f4ab82a52a1c`); evidence = Phase 5 seed-42
   artifact `elasticity-hier-7aeb4dc45d43`. **Gate FAIL** (below). Not re-run.
4. **Second amendment** (2026-10-07; diagnosed on seed-42 truth, developed on seed 1 only): the
   churn evidence became a RELATIVE effect (log churn-rate ratio per unit log price, pooled),
   scaled at decision time by the churn rate observed just before the decision. Dev world: 40
   holds, all gated checks pass. Seed 42 was NOT re-run with it.
5. **Seed 43**, pre-registered in ADR 0013 and `shadow_acceptance.toml` (comment only; thresholds
   unchanged, file re-pinned) before any seed-43 world existed; each input world built once
   through its own gate: elasticity experiment world (8,000 customers; truth-free validity and
   Bayesian gates PASS, artifact `elasticity-hier-83deccf0476d`), forecast world (backtest
   acceptance PASS, artifact `demand-hybrid-a601f13e68ef`), pricing shadow world. Shadow
   evaluation run once: **PASS**.

## Results: held-out re-evaluation (seed 43, final method; decides the gate)

40 decisions, all `hold` (`no_improvement`: the risk-adjusted objective of every move was <= 0).

| Check (gated) | Result | Threshold |
| --- | --- | --- |
| constraint compliance | 0 violations | 0 |
| shadow executions | 0 | 0 |
| complete audit records | 40 / 40 | all |
| forecast lineage | prefix = artifact data version | required |
| no harm | 0 (no price moved) | >= 0 |
| direction | vacuous (0 changes) | >= 0.80 of changes |
| prediction calibration | not judged (< 5 changes) | [0.5, 2] |
| churn guardrail in truth | no change | <= 0.5 pp |
| stress cases fail safe | 5 / 5 | all |

Reported: change share 0; best feasible candidates would have gained +GBP 22 per day in total
(missed). Naive unconstrained baseline: -GBP 784 k per day.

**The transport fix, checked against truth** (api_requests, +5%; truth computed after the run):

| Cycle | Recorded churn slope (relative x observed rate) | True slope | z |
| --- | --- | --- | --- |
| 2026-07-20 | 0.050 +- 0.029 (0.758 x 0.066) | 0.034 | -0.56 |
| 2026-08-10 | 0.056 +- 0.032 (0.758 x 0.074) | 0.030 | -0.81 |
| 2026-09-07 | 0.070 +- 0.040 (0.758 x 0.093) | 0.044 | -0.66 |

All 8 cycles: truth within 1 SD (z -0.33 to -0.98). The estimate errs high (the observed churn
rate includes involuntary payment churn, which price does not drive): the safe direction.
Compare seed 42 with the absolute model: estimate 0.0017 vs truth 0.031-0.052.

**Phase 5 replication note (seed-43 experiment world).** Truth-free gates passed and every
quantity the optimiser consumes recovered (sign, pooled magnitude, tier magnitude, segment
ordering), but two CELL-level recovery checks failed: 12 / 18 cell 90% intervals covered
(threshold 0.70) and pooling raised cell RMSE (0.098 vs 0.086 unpooled). The optimiser uses tier
estimates only. Not a Phase 6 gate item; recorded for the owner (Phase 5 cell-level claims did
not replicate on a fresh seed).

## Results: first held-out shadow world (seed 42, first method; FAILED; 40 decisions)

Decisions: 17 change (api_requests x8, premium_latency x8, data_transfer_gb x1), 15 frozen
(P(improve) 0.76-0.90), 8 hold (gpu_minutes, no improvement). Changes bound by max step (16) and
the extrapolation limit (1).

| Check (gated) | Result | Threshold |
| --- | --- | --- |
| constraint compliance (re-checked from records) | 0 violations | 0 |
| shadow executions | 0 | 0 |
| complete audit records | 40 / 40 | all |
| forecast lineage | prefix = artifact data version | required |
| **no harm** | **sum of true objective deltas -GBP 3,526 per day** | >= 0 |
| **direction** | **4 / 17 changes improved the true objective (0.24)** | >= 0.80 |
| prediction calibration (contribution) | predicted GBP 5,872 vs true GBP 5,645 per day: ratio **1.04** | [0.5, 2] |
| churn guardrail in truth | max true extra churn 0.32 pp per exposed customer / 28 days | <= 0.5 pp |
| stress cases fail safe | 5 / 5 (stale features, features too old, model unavailable, evidence unavailable, inflated uncertainty) | all |

Reported: change share 0.43; best feasible candidates would have gained +GBP 74 per day in
total; the chosen prices lost GBP 3,526 per day. True deltas per change (GBP per day): api
-203 ... -570 (all 8 negative, worsening over the cycles); premium +28, +27, -3, +9, +8, -91,
-126, -99; data transfer -338.

Naive baseline (point-estimate contribution optimum, no constraints, no churn): x3.0 the
current price on every decision, violating ceiling, max step, extrapolation and the churn
guardrail every time; true extra churn up to 22% per exposed customer per 28 days; true
objective -GBP 1.06 M per day. The constraints prevent that failure entirely.

### Why it failed (diagnosed on the held-out truth after the run; nothing re-run)

| | Experiment window (days 14-49) | Shadow window (days 200-245) |
| --- | --- | --- |
| Mean daily voluntary-churn hazard (truth) | 0.00115 | 0.0017-0.0028 |
| True api churn slope (per unit log price, 28 days) | ~0.022 | 0.031-0.052 |
| Mean regional utilisation (marts) | 0.59 | 0.90-0.96 |

1. **Noise.** The seed-42 Phase 5 tests estimate the pooled churn slope at 0.0017 +- 0.016,
   ~1.3 SD below the truth in their own window. With that draw, api and premium raises cleared
   P(improve) >= 0.90.
2. **Transport bias.** Price multiplies the churn HAZARD. Demand growth pushes utilisation from
   ~0.6 to ~0.95, degraded service raises the base hazard, so an absolute churn slope measured in
   February understates August's by 1.5-2.3x. The confidence rule models sampling noise only, so
   it cannot see this. It also explains why the losses grow cycle by cycle.
3. The **guardrail's 95% upper bound** (max projected 0.14 pp) was below the truth (0.32 pp). The
   guardrail limit (0.5 pp) still held, because the step is small. It is not a safe bound under
   transport.
4. **The development world passed partly by chance**: its churn estimate (0.012) was a high draw,
   so the same method froze there.

Contribution modelling is not the problem: forecast + causal elasticity predicted the
contribution change of the chosen prices within 4% (2% on dev).

## Results: development world (seed 1)

First amendment (absolute pooled slope): 23 hold, 17 frozen. Final method (relative slope): 40
hold. All gated checks pass both times (no harm 0; direction and calibration vacuous). Missed:
+GBP 183 per day (premium raises). Naive baseline: -GBP 891 k per day. A full rebuild of the dev
world by `nightly-science` reproduces every decision number.

## Tests executed

`make check` exit 0 (2026-10-07, final code): 973 passed, 1 skipped (live BigQuery), 11 m 23 s; coverage floors
met for 103 files (all of `praxis.pricing` >= 95%); 4 import contracts kept; workflow lint clean.
`make nightly-science` exit 0 (final code, 11 m 19 s; dev shadow report byte-identical to the archived one): forecast, elasticity, contamination and the Phase 6 dev
shadow pass; the rebuilt dev world reproduced every decision number (only the elasticity
artifact's `code_revision`, hence ids, changed).

Also fixed during the gate: (1) `stg_marginal_costs` used a compile-time `run_query`, which broke
the offline BigQuery compile; it now reads the `marginal_cost_products` dbt var (guarded by a
test against the simulator catalogue). (2) The 10K scale smoke failed its RSS budget inside the
full suite: Linux keeps `ru_maxrss` across fork + exec, so the child reported pytest's peak
(1,546 MB; alone 220 MB). `peak_rss_mb` now reads `VmHWM`; regression test added.

| Required (required_test.md s12) | Test |
| --- | --- |
| Golden cases vs brute force | `test_golden.py`: Lerner optimum (3 cases), step / ceiling / extrapolation / churn guardrail / capacity / margin-floor-forced / cooldown binding cases, 12 random problems vs exhaustive tick enumeration (`tests/pricing/reference.py`, independent scalar implementation) |
| Property: bounds, max change, cooldown, capacity, margin floor, churn guardrail, uncertainty freeze | `test_properties.py` (Hypothesis, 150 cases each): every returned price passes every constraint of the reference; unforced changes are confident and improve; cooldown and SD freeze always respected; decisions deterministic |
| Failure: infeasible constraints | `test_infeasible_constraints_fail_safely`, stress |
| Failure: missing forecast | `test_builds_a_problem...` (gpu without series -> `forecast_missing`) |
| Failure: stale forecast | `test_unavailable_inputs_block_every_product[forecast_stale / forecast_unavailable]`; stress `stale_features`, `features_too_old` |
| Failure: model unavailable | `[model_unavailable]` (artifact error, evidence missing); stress cases |
| Failure: cost unavailable | `test_missing_cost_or_price_is_explicit` |
| Failure: NaN / infinite objective | `test_infinite_objective_is_refused`; NaN / inf inputs in `test_impossible_inputs_are_refused` |
| Failure: negative / impossible inputs | `test_impossible_inputs_are_refused` (23 cases) |
| Audit: no executable decision without a persisted record | `test_store.py` (memory + Postgres): unrecorded never executes, shadow never, recommend needs approval, only changes, idempotent; DB trigger refuses raw-SQL executions without record / wrong price / shadow / unapproved; append-only tables; tampered record fails checksum. `test_service.py`: unrecordable decision never executable |

Also: market snapshot exactness and no future data (`test_inputs.py`, poisoned future rows),
evidence joins and refusals (`test_evidence.py`), pooling estimator, record -> problem
reproducibility (re-optimising a record gives the identical record), independent compliance
re-check catching 8 doctored records, truth collector, simulator observer (event stream
unchanged with an observer; expected demand matches realised within 3%; counterfactual demand
follows the latent elasticity exactly; own-price only), hash neutrality of
`arrival_horizon_days`, population identity of the two worlds, end-to-end
(`test_pipeline.py`: simulator -> dbt incl. the new cost mart -> forecast artifact on the prefix
-> pricing CLI in shadow, execute and recommend modes against Postgres -> shadow CLI).

## Performance

1,000 customers x 252 days: simulate + load + dbt 27 s (764 k events). Shadow evaluation (8
cycles incl. forecast service, 5 stress cycles, truth for 1,688 candidate prices + 40 naive
prices x 7 days each) 12 s. One optimiser call: 6.6 ms mean (41-point grid, 199 x 21
uncertainty grid, refinement; up to 65 candidates per decision).

Reproducibility: every one of the 40 held-out records is reproduced byte for byte by
re-optimising its own recorded inputs (`problem_from_record`); three dev runs gave identical
outputs.

## Failures found and fixes

* Registered churn treatment (point slopes, uncertainty ignored) -> winner's curse on dev ->
  method amendment (ADR 0013).
* Registered acceptance presupposed movement -> owner-approved amendment before seed 42.
* Non-deterministic float aggregation in the CLV proxy -> exact integer aggregation (regression
  test with negative control).
* Boundary optima missed by one tick (sampled refinement) -> dense polish stage (golden test).
* Captured share with nothing to gain reported 1.0 even when value was lost -> defined as 0 then.
* JSONB renormalised floats and broke record checksums -> records stored as JSON text.
* Stale seed-1 forecast artifact -> refused by the lineage guard; retrained.
* Absolute churn slope not transportable across churn regimes -> relative effect scaled by the
  observed churn rate (second amendment); held-out failure on seed 42, pass on seed 43.

## Known limitations

* **The optimiser does not yet move prices.** The relative churn effect is measured to about
  +-0.4 to 0.5 (pooled), so at the CLV-proxy valuation no move is confidently profitable and
  every decision holds. Missed value is small in this world (GBP 22-183 per day). A powered churn
  experiment, a churn / CLV model (Phase 9) or a different valuation would let it act; any such
  change needs a new pre-registered held-out seed (42 and 43 are spent for pricing). **Assigned to
  Phase 9** (owner decision 2026-10-07; work items in `project_progress.md`).
* The observed churn rate used for scaling includes involuntary churn (conservative).
* CLV is a proxy (daily contribution x min(1 / hazard, 365 days)); it makes churn the dominant
  term. Phase 9 replaces it.
* Constant elasticity by construction in the simulator; moves are capped at 5% per cycle and 18%
  from the tested price.
* Truth is first order: each decision is scored against the world's actual customers and service
  state (the counterfactual's own effect on capacity and later churn is not fed back).
* No API endpoint for pricing yet (CLI + service only); execution exists only in tests (no live
  price book consumer). Cross-price effects assumed absent.

## Cloud cost incurred

GBP 0. Local DuckDB, dbt, Postgres (Docker) only.

## Reason

Every project_plan gate item has evidence: golden and brute-force cases, property tests showing
no constraint is ever violated, safe failure for infeasible problems and every missing / stale /
invalid input, a database-enforced rule that nothing executes without a persisted audit record,
uncertainty freezes, and a complete shadow-mode evaluation against simulator truth. The first
held-out evaluation (seed 42) failed and is kept as evidence; its cause (an absolute churn effect
transported into a higher-hazard regime) was diagnosed from truth, fixed on the development world
only, and re-evaluated once on a seed pre-registered before it was built, with unchanged
thresholds. On seed 43 every gated check passes and the churn effect the optimiser used lies
within 1 SD of the truth on every cycle.

Caveat, stated plainly: the pass shows a SAFE optimiser (no harm, honest uncertainty, correct
holds), not a profitable one; it made no price change on seed 43. The policy remains in shadow
mode.
