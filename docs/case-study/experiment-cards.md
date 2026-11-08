# Experiment cards

One card per final experiment in `project_plan.md` Phase 16. Every result is **cited** from the
evidence file named on its card. **No held-out evaluation was re-run in Phase 16** (the
integrity rule: a held-out seed is evaluated once). The worlds are SYNTHETIC simulator worlds.

Columns used on every card:
- **Pre-registration**: the pinned config that held the acceptance before the run (hash-pinned
  by a test).
- **World**: the seed, and whether it was held-out (H) or development (D).
- **Verdict**: as recorded, including failures.

---

## E1. Forecast benchmark

| | |
| --- | --- |
| Question | Does the demand forecaster beat the documented naive baselines out of time? |
| Pre-registration | `configs/forecast/demand.toml` `[acceptance]` (ADR 0010, re-registered by ADR 0011 **before** seed 42 was run) |
| World | Seed 42 (H): 1,000 customers, 196 days, 7 unannounced spikes, 4 price changes, 1 capacity shock. 16 weekly origins x 72 series x 7 horizons = 8,064 forecasts |
| Result | Hybrid vWAPE 0.1288. Seasonal naive 0.1661, seasonal moving average 0.1401, LightGBM 0.1357. Ratio vs naive **0.776** (limit 0.90). Bootstrap CI of the difference [-0.0455, -0.0292]. Worst slice 0.812 (limit 1.10) |
| Verdict | **PASS 11 / 11** |
| Caveats | No better than naive on spike days (0.398 vs 0.394). Real external signals gave no lift (0.1288 to 0.1304), as expected, because the simulator's demand does not depend on them |
| Evidence | [phase-4.md](../evidence/phase-4.md), [backtest](../evidence/phase-4-backtest-eval-seed42.json), [ablation](../evidence/phase-4-ablation-external-seed42.json), [dev](../evidence/phase-4-backtest-dev-seed1.json) |

## E2. Interval calibration

| | |
| --- | --- |
| Question | Do the prediction intervals cover at their nominal rate? |
| Pre-registration | Same file. 80 % coverage must fall in [0.74, 0.86] and 50 % in [0.43, 0.57] |
| World | Seed 42 (H) |
| Result | 50 / 80 / 90 % coverage = **0.493 / 0.795 / 0.897** pooled. Before rearrangement, 7.8 % of rows had crossing quantiles; after it, 0 |
| Verdict | **PASS** |
| Caveats | Spike days 0.272 (80 %) on 162 rows. Enterprise 0.745, the small regions 0.76-0.78. Calibration was added after the dev world showed in-sample under-coverage (about 0.71), and that was before seed 42 |
| Evidence | [phase-4.md](../evidence/phase-4.md) "Slices" |

## E3. Elasticity parameter recovery

| | |
| --- | --- |
| Question | Do randomised price tests plus the hierarchical model recover the simulator's latent elasticities? |
| Pre-registration | `configs/elasticity/acceptance.toml` (five Phase 5 files hash-pinned) |
| World | Seed 42 (H): 8,000 customers, 1,513,024 events. Also a contamination world, seed 42 (H) |
| Result | Pooled -1.3335 vs truth -1.3315 (0.15 %, z = 0.10). Tiers within 5.1 %. 14 / 14 orderings correct. Cell coverage 17 / 18. Contamination detected at 0.200 / 0.189; IV within 0.5 % |
| Verdict | **PASS 8 / 8** |
| Caveats | The seed-43 replication: the tier and pooled quantities replicated, the **cell-level checks did not** (12 / 18 coverage) |
| Evidence | [phase-5.md](../evidence/phase-5.md), [seed 42](../evidence/phase-5-eval-seed42-recovery.json), [contamination](../evidence/phase-5-contamination-seed42-recovery.json), [seed 43](../evidence/phase-6-seed43-elasticity-recovery.json) |

## E4. Optimiser vs static pricing

| | |
| --- | --- |
| Question | Does the constrained optimiser beat holding prices static (true objective delta >= 0, changes in the right direction), and does it beat a naive optimiser? |
| Pre-registration | `configs/pricing/shadow_acceptance.toml`, `policy.toml` (amended with owner approval before seed 42; seeds 43, 44, 45, 201-203 and 301-303 each registered before they were built) |
| Worlds (all H, each run once) | Seed 42: **FAIL**. 17 changes, 4 right, -GBP 3,526 / day vs static. Seed 43: PASS, 40 holds, delta 0. Seed 44: **FAIL**, 5 changes, 0 right, -GBP 81 / day. Seed 45: **FAIL** on `direction`, 4 / 7 right, +GBP 13.9 / day. Seeds 201 and 203: PASS, 0 changes. Seed 202: **FAIL**, 9 changes, 3 right, -GBP 49 / day. Seeds 301 / 302 / 303: **PASS 3 / 3**, all holds and freezes, delta 0 |
| Naive comparison | The unconstrained point optimum (about 3x the price) loses -GBP 287 k to -GBP 1.06 M / day in truth. The constraints prevent that every time |
| Verdict | Phase 6 PASS on seed 43 (after the seed-42 FAIL). Phase 9 PASS on 301-303 (after the FAILs on 44, 45 and 202) |
| **What it does NOT show** | **The optimiser has not been shown to beat static pricing on any held-out world.** Every held-out pass is holds and freezes: no held-out price moved. Value-adding moves appear only on DEVELOPMENT worlds (22-world sweep: 15 changes, 13 right, +GBP 804 / day). Measured with the pre-Phase-10 forecast-loader defect |
| **Fresh evaluation (final run, ADR 0031)** | Fixed code, unchanged method, seeds **901-905** registered first and run once. 15 changes, 9 right. Mean **+GBP 34.3 / day, 95 % bootstrap CI [-113.9, +203.9]**. Seed 905 **-GBP 210.9 / day** (churn on `data_transfer_gb` raises under-predicted). **H1 FAIL, H2 FAIL, H3 PASS.** 0 constraint violations, 0 executions on every world. Still not shown to beat static pricing |
| Evidence | [final-pricing-evaluation.md](../evidence/final-pricing-evaluation.md), [phase-6.md](../evidence/phase-6.md), [phase-9.md](../evidence/phase-9.md), `phase-6-eval-seed4{2,3}-shadow.json`, `phase-9-eval-seed4{4,5}-shadow.json`, `phase-9-heldout-seed{201,202,203,301,302,303}-shadow.json`, [dev sweep](../evidence/phase-9-dev-loo-sweep.json) |

## E5. Recovery model vs fixed retry baseline

| | |
| --- | --- |
| Question | Does model-planned dunning beat the fixed schedule (retry +3 d, +7 d, then suspend) on business value? |
| Pre-registration | `configs/recovery/acceptance.toml` and `policy.toml` (one owner-approved amendment on dev before seed 42) |
| World | Seed 42 (H): 8,000 customers x 180 days. Closed-loop replay against latent cure times, 445 episodes |
| Result | Net value **+GBP 47.72 / episode, paired 95 % CI [+21.98, +75.99]**. Recovery 61.1 % vs 54.6 %. Recovered revenue x1.057. Failed retries 0.84 vs 1.22. Restricted days 2.1 vs 5.3. 98.5 % of oracle value captured vs 93.6 % |
| Verdict | **PASS 14 / 14** |
| Caveats | Slower recovery (9.8 vs 6.9 days). The raw ECE of 0.034 would have failed the original 0.03 gate. The replay does not feed failed retries back into churn |
| Evidence | [phase-8.md](../evidence/phase-8.md), [evaluation](../evidence/phase-8-eval-seed42-evaluation.json) |

## E6. CLV / churn guardrail effect

| | |
| --- | --- |
| Question | Do the long-horizon guardrails change unsafe short-term decisions? |
| Pre-registration | `configs/customer/value.toml`, `policy.toml` `[long_term]` (ADR 0016) |
| World | Controlled example: the Phase 6 seed-42 world (H, already spent), replayed with the final method. This is a **demonstration on a known failure, not a fresh held-out test** |
| Result | **17 / 17** Phase 6 changes altered, **13 / 13** losing changes removed. 32 holds, 8 freezes, all gated checks pass, true delta 0 (Phase 6: -GBP 3,526 / day) |
| Verdict | **PASS** (project_plan gate item) |
| Caveats | The guardrails stop losses on held-out worlds. They have not been shown to permit gains there (see E4) |
| Evidence | [phase-9.md](../evidence/phase-9.md), [controlled shadow](../evidence/phase-9-seed42-controlled-shadow.json), [compare](../evidence/phase-9-seed42-controlled-compare.json) |

## E7. Model rollback demonstration

| | |
| --- | --- |
| Question | Does the lifecycle reject a bad candidate, detect drift, and restore the previous champion when a promoted model degrades? |
| Pre-registration | `configs/mlops/lifecycle_acceptance.toml`, `configs/mlops/demand.toml`, `mlops_drift.toml` |
| Worlds | Attempt 1, seed **601** (H): **FAIL** on 1 / 12 checks (`steady_max_decision_alarm_days`: 30 / 57 alarm days against a limit of 0). Attempt 2, seed **621** (H, fresh, code frozen first): **PASS 12 / 12** |
| Result (621) | Forced bad champion restored on 08-23 (ratio 1.082 > 1.05), and the router served the previous version. Bad recipe rejected (1.142 vs 1.02). First drift alarm on 08-09 against a deadline of 08-10 (**1 day of slack**). Steady world: 0 alarm days. Lineage complete for 16 versions |
| Verdict | **PASS** (attempt 2). The attempt-1 FAIL stands |
| Caveats | The attempt-2 steady world removes organic growth: concentrated organic demand is a documented monitoring limitation. **Automatic rollback has not happened in the cloud**, because it needs a promotion first |
| Evidence | [phase-10.md](../evidence/phase-10.md), [seed 621](../evidence/phase-10-attempt2-eval-seed621-drill.json), [seed 601](../evidence/phase-10-eval-seed601-drill.json) |

## E8. Agent incident benchmark

| | |
| --- | --- |
| Question | Does the AI operator diagnose fixed incidents correctly, ground its answers, and never execute a prohibited action? |
| Pre-registration | `configs/agent/benchmark.toml` and `policy.toml` (pinned) |
| World | 7 fixed scenarios x 2 providers, plus adversarial "obedient" runs |
| Result | **7 / 7**. 0 prohibited executions. Audit completeness 1.0. 28 prohibited attempts refused |
| Verdict | **PASS** (Phase 12). Re-run in Phase 16: 7 / 7 again ([phase-16-agent-benchmark.json](../evidence/phase-16-agent-benchmark.json)) |
| **Caveat** | **The benchmark scores the deterministic triage investigator, NOT an LLM.** The LLM provider was decided as Gemini through a structured-step protocol (ADR 0029). Offline, the same 7 scenarios pass through the protocol with an echo client (7 / 7, 0 repairs, 0 fallbacks: [final-agent-llm-protocol.json](../evidence/final-agent-llm-protocol.json)). That tests the integration, not a model. **No live model has been scored** (no API key) |
| Evidence | [phase-12.md](../evidence/phase-12.md), [benchmark](../evidence/phase-12-benchmark.json) |

## E9. Scale: 100K, the M1-budget ladder, and 1M

| | |
| --- | --- |
| Question | Does the event backbone stay correct, and inside its SLOs, at 100K and 1M virtual customers? |
| Pre-registration | `configs/scale/slo.toml` (ADR 0022). Sim seed 1, fault seed 701 |
| Result (100K) | 9,737,517 events, 2,166 s, **4,494.6 ev/s** (SLO >= 2,700). Operational apply p99 21.3 ms (<= 50). Host peak 10,959 MB (<= 13,000). 0 open dead letters. **Final state = oracle**, ledger = oracle, 0 deadlocks. **10 / 10 SLOs.** HTTP at 100K and payments at 10K also pass |
| Result (1M) | **NOT RUN.** The DuckDB primary-key index needs about 180 MB per million rows, so 1M would need about 17 GB on a 14 GB host. **The Phase 14 gate did not pass** |
| Phase 16 | 100K was **cited, not re-run** (it peaks near the host's free memory) |
| Final run: M1-budget ladder (ADR 0028) | Registered 2026-11-05, before any of its runs. It **emulates** an 8 GB Apple M1 on Linux: the harness in a 4 GB, no-swap cgroup on 8 CPUs, and Postgres in a 1 GB container. The warehouse has no primary-key index (landing table plus compaction). On the final warehouse code: **m1-100k PASS 12/12** (5,141 ev/s, p99 13.3 ms, final state identical to Phase 14's), **m1-250k PASS 12/12** (24.3M events, 5,146 ev/s), **m1-500k PASS 12/12** (48.7M events, 4,370 ev/s, p99 16.5 ms, 0 OOM kills). In the 500K run, Postgres swap was uncapped after a host reboot (14.5 MB swapped), so **250K is the largest step fully inside the registered budget**. **1M was not run** (owner decision 2026-11-06). Each step ran once. Not an M1, not the cloud |
| Evidence | [phase-14.md](../evidence/phase-14.md), [W1](../evidence/phase-14-100k-w1.json), [W2](../evidence/phase-14-100k-w2.json), [HTTP](../evidence/phase-14-100k-http.json), [payments](../evidence/phase-14-payments-10k.json), **[final-scale.md](../evidence/final-scale.md)** |

## E10. Cost

| | |
| --- | --- |
| Question | Does the system run within the owner's budget? |
| Pre-registration | Ceiling GBP 10 / month. Budget alerts at 2 / 5 / 8 / 10. Spend guard unlinks billing at GBP 8 (ADR 0026) |
| Result | Estimates: GBP 0.10-0.20 / month for the deployment (plausible bad case about GBP 8), plus about USD 0.5-3 / month for the live loop (bounded by ADR 0030). **Measured 2026-11-05: GBP 0.65 month-to-date**, from Google's budget notification; the spend guard held. See [cost-report.md](cost-report.md) |
| Verdict | Within the ceiling on the measured figure. It is not an invoice, and there is no per-service breakdown |
| Evidence | [cost-guard.md](../cost-guard.md), [deployment-preflight.md](../evidence/deployment-preflight.md), [ADR 0027](../adr/0027-live-loop.md) |
