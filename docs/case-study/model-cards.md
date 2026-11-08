# Model cards

Four model families feed decisions. Every number here was measured on **SYNTHETIC** simulator
worlds. None of these models has been evaluated on real customers. "Held-out" means a seed
that was pre-registered, built once and evaluated once (CLAUDE.md s24). "Dev" means the
development world, seed 1.

---

## 1. Demand forecaster: `demand-hybrid-*` (Phase 4, ADR 0010 / 0011)

| | |
| --- | --- |
| Purpose | Daily demand per (region, product, segment) series, horizons 1-7 days, with 50 / 80 / 90 % intervals. Feeds pricing and drift monitoring |
| Architecture | Hybrid champion: the ridge-regression mean plus LightGBM quantiles (q0.05-q0.95) with out-of-sample calibration (28 calibration days); rearranged so quantiles never cross |
| Training data | Warehouse panel (dbt `feat_region_daily` and marts). 72 series, 83,160 rows in the held-out training run |
| Features | `demand_features.v1`: lags, calendar, planned price on the target day only, `initial_tier` (never `current_tier`). External signals are off: they gave no lift (see below) |
| Validation | Rolling-origin backtest, 16 weekly origins, no random split. Block-bootstrap CI. Pre-registered acceptance pinned by a test |
| Held-out result (seed 42) | vWAPE **0.1288** vs seasonal naive 0.1661 (ratio **0.776**; 95 % CI of the difference [-0.0455, -0.0292]). Pinball 0.0397 (ratio 0.770). Coverage 50 / 80 / 90 % = **0.493 / 0.795 / 0.897**. 11 / 11 checks PASS |
| Dev reproduction | vWAPE ratio 0.775 (seed 1). Re-run in Phase 16 by `make nightly-science`: see [phase-16.md](../evidence/phase-16.md) |
| Known failure modes | **Spike days cannot be forecast**: vWAPE 0.398 vs naive 0.394, and 80 % coverage **0.272** on 162 rows. Enterprise 80 % coverage 0.745 sits at the edge of the band. The small regions under-cover (0.76-0.78) |
| Serving contract | Fresh (feature lag <= 1 day): model. Stale (2-7 days): seasonal fallback, labelled. Older than 7 days: 503 `features_unavailable`. Pricing holds every price on a non-fresh forecast |
| Lifecycle | Registry stages challenger, shadow, READY, champion (named approver), probation, then automatic restore. Drift opens retraining requests. Retraining never implies promotion |
| Disclosed defect | Before Phase 10, a LOADED artifact applied its one-hot coefficients in the wrong order. Dev vWAPE was 0.1623 with the defect and 0.1558 fixed. Fixed and regression-tested (`serving_parity`). The Phase 6 and 9 pricing results stand as measured with the defect |
| Evidence | [phase-4.md](../evidence/phase-4.md), [backtest seed 42](../evidence/phase-4-backtest-eval-seed42.json), [ablation](../evidence/phase-4-ablation-external-seed42.json), [manifest](../evidence/phase-4-artifact-manifest.json), [phase-10.md](../evidence/phase-10.md) |

---

## 2. Price elasticity: `elasticity-hier-*` (Phase 5, ADR 0012)

| | |
| --- | --- |
| Purpose | The causal own-price elasticity of demand per tier and industry. The optimiser consumes the tier estimates and their SD |
| Identification | Randomised price tests: 5 products, salted assignment, SRM / balance / contamination validity gates. A pre/post comparison is never used |
| Architecture | Log-log ITT slopes per cell, then a two-stage hierarchical Bayesian model (PyMC, 4 chains, fixed seed, bit-reproducible). IV estimator when contamination is detected |
| Held-out result (seed 42, 8,000 customers) | Pooled **-1.3335 (SE 0.0192) vs truth -1.3315** (0.15 % error). Tiers within 0.7-5.1 %. 14 / 14 identifiable segment orderings correct. Cell 90 % coverage 17 / 18. Hierarchical RMSE 0.0668 vs unpooled 0.0717. R-hat max 1.0017, 0 divergences. 8 / 8 checks PASS |
| Contamination world (seed 42) | Detected 0.200 / 0.189 against 0.20 configured. IV pooled -1.338 vs truth -1.332. The artifact was correctly refused |
| Replication caveat | On the fresh seed-43 experiment world, every quantity the optimiser uses was recovered (sign, pooled, tier, ordering). Two CELL-level checks failed: 12 / 18 cell coverage, and pooling raised cell RMSE. The cell-level claims did not replicate |
| Known failure modes | The naive pre/post estimate is biased by trend (for example api -0.970 vs ITT -1.244). Constant elasticity holds by construction in the simulator. Cross-price effects are assumed absent |
| Evidence | [phase-5.md](../evidence/phase-5.md), [seed 42 recovery](../evidence/phase-5-eval-seed42-recovery.json), [contamination](../evidence/phase-5-contamination-seed42-recovery.json), [seed 43 recovery](../evidence/phase-6-seed43-elasticity-recovery.json) |

---

## 3. Payment recovery / dunning: `recovery-surv-*` (Phase 8, ADR 0015)

| | |
| --- | --- |
| Purpose | P(cure by time t) for a failed invoice, used to plan up to 3 retries, each worth its expected value |
| Identification | Retry gaps randomised over {1, 2, 3, 4, 5, 7, 10} days in the simulator. Censoring is non-informative by design (gap uniform p 0.84, independent of reason p 0.63) |
| Architecture | Champion: a mixture-cure Weibull survival model, interval-censored MLE with an analytic gradient. Challenger: monotone LightGBM plus isotonic calibration. Baseline: a reason x gap rate table, with the deterministic schedule retry +3 d, then +7 d, then suspend |
| Held-out result (seed 42, 445 episodes, run once) | Brier 0.1648 (table 0.1659, base rate 0.2149). ECE 0.034 (perfect-calibration noise p95 0.064). PR-AUC 0.651. **Net value +GBP 47.72 per episode vs the fixed schedule, paired 95 % CI [+21.98, +75.99]**. Recovery 61.1 % vs 54.6 %. Failed retries 0.84 vs 1.22 per episode. Restricted days 2.1 vs 5.3. 14 / 14 PASS |
| Trade-off (reported) | Mean time to recovery is 9.8 days vs 6.9, because the policy waits for late cures. The classifier challenger lost to the baseline (captured 90.2 % of oracle value vs 93.6 %) |
| Amendment | Owner-approved before seed 42. ECE <= 0.03 was unattainable at about 500 rows, so the gate became "consistent with perfect calibration". On seed 42 the raw ECE was 0.034, so **the original gate would have failed** |
| Known failure modes | Dev world: the insufficient_funds truth error is 0.13, a documented known failure in nightly science. Small reasons are reported, not gated (authentication_required error 0.137 on 18 episodes) |
| Fallback | Missing, corrupt, stale (> 45 d) or unknown input falls back to the deterministic baseline, and the reason is recorded. The fallback equalled the baseline on all 445 episodes |
| Evidence | [phase-8.md](../evidence/phase-8.md), [evaluation seed 42](../evidence/phase-8-eval-seed42-evaluation.json), [training](../evidence/phase-8-eval-seed42-train.json), [manifest](../evidence/phase-8-artifact-manifest.json) |

---

## 4. Churn hazard and CLV: `churn-table-*` (Phase 9, ADR 0016)

| | |
| --- | --- |
| Purpose | Voluntary churn hazard per segment, and CLV, for the long-horizon guardrails. Also the bill response: how voluntary churn reacts to a product's price, weighted by its share of the bill |
| Architecture | A Poisson GLM (piecewise exponential) vs a tier rate table. The champion is chosen by rolling-origin deviance. CLV = margin x (1 - payment loss) x the sum over t = 1..365 of 1.10^(-t/365) exp(-(h_vol + h_inv) t). The bill response comes from the randomised tests, pooled with a robust meta-analytic prior built from 25 dev experiments' **estimates**, never truth |
| Label | Voluntary churn in [T, T + 28 d). Involuntary churn is a competing risk. Tier as of T |
| Result (held-out seed 44, reported) | Champion: the tier rate table. Calibrated hazards vs TRUE latent hazards: log correlation 0.78-0.83. Over-predicts enterprise (1.31-2.18x, the conservative direction). Predicted vs true extra churners 0.91 |
| Validation | CLV hand-calculated fixtures and closed form = explicit sum to 1e-12. Leakage test on a poisoned-future warehouse. Sensitivity grid of 4 discount rates x 5 horizons |
| Known failure modes | The bill-share weighting understated gpu's churn response by about 2.7x on seed 44 (recorded in ADR 0016 before the run). When a single noisy test reads near zero, absence of evidence was treated as evidence of absence (seed 202). The archive prior is the mitigation |
| Evidence | [phase-9.md](../evidence/phase-9.md), [seed 44 customer train](../evidence/phase-9-eval-seed44-customer-train.json), [seed 44 monitor](../evidence/phase-9-eval-seed44-customer-monitor.json), [manifest](../evidence/phase-9-artifact-manifest.json) |
