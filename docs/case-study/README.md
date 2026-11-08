# Praxis case study

**Praxis** is a continuously operating decision system for a *simulated* API and cloud-compute
business. It forecasts demand, estimates causal price elasticity, sets prices under hard
guardrails, plans payment-recovery retries, retrains and safely promotes its own models,
watches itself through SLOs and incidents, and offers a constrained AI operator that can
investigate but never act alone. It is deployed on GCP and runs a daily live loop.

This page is the entry point for reviewers. Every number on it links to the evidence file it
came from. **Failures are reported alongside passes, in the same place.**

> **Read first.** All customers, demand, revenue, churn, payments, recovery and uplift are
> **SYNTHETIC**, produced by the Praxis simulator. **Nothing here is a real company's revenue
> or a real-world business result.** "GBP" amounts are simulated units.

## 1. Evidence by kind

The final gate requires the case study to keep these six kinds of evidence apart. The
sections below do that.

### 1.1 Simulated results (SYNTHETIC worlds, scored against simulator ground truth)

| Experiment | Held-out result (run once) | Verdict | Card |
| --- | --- | --- | --- |
| Forecast benchmark | vWAPE 0.1288 vs seasonal naive 0.1661 (ratio 0.776, CI of the difference [-0.0455, -0.0292]), seed 42 | PASS 11 / 11 | [E1](experiment-cards.md#e1-forecast-benchmark) |
| Interval calibration | Coverage 50 / 80 / 90 % = 0.493 / 0.795 / 0.897. Spike days 0.272 | PASS | [E2](experiment-cards.md#e2-interval-calibration) |
| Elasticity recovery | Pooled -1.3335 vs truth -1.3315 (0.15 %). 14 / 14 orderings. Seed 42 | PASS 8 / 8 (cell level did not replicate on seed 43) | [E3](experiment-cards.md#e3-elasticity-parameter-recovery) |
| Optimiser vs static | FAIL on seeds 42, 44, 45 and 202. PASS on 43, 201, 203 and 301-303. **Every held-out pass is holds and freezes: no held-out price moved** | Phase 6 and Phase 9 gates PASS after amendments; **not shown to beat static on any held-out world** | [E4](experiment-cards.md#e4-optimiser-vs-static-pricing) |
| Recovery vs fixed retry | +GBP 47.72 / episode, 95 % CI [+21.98, +75.99]. Recovery 61.1 % vs 54.6 %. Seed 42 | PASS 14 / 14 (after an owner-approved amendment) | [E5](experiment-cards.md#e5-recovery-model-vs-fixed-retry-baseline) |
| CLV / churn guardrail effect | Altered 17 / 17 of the Phase 6 seed-42 changes, removing all 13 losing ones | PASS (controlled example) | [E6](experiment-cards.md#e6-clv--churn-guardrail-effect) |
| Model rollback | Seed 601: **FAIL** (1 / 12). Seed 621: 12 / 12, including a forced bad champion restored | PASS on attempt 2 | [E7](experiment-cards.md#e7-model-rollback-demonstration) |
| Agent incident benchmark | 7 / 7, 0 prohibited executions. **The deterministic triage investigator, NOT an LLM** | PASS | [E8](experiment-cards.md#e8-agent-incident-benchmark) |
| **Fresh held-out pricing evaluation (final run)** | Seeds 901-905, pre-registered, run once. Mean +34.3 GBP/day vs static, 95 % CI [-113.9, +203.9]. Seed 905 lost 210.9 GBP/day (churn under-predicted); 901 failed direction. 0 constraint violations, 0 executions | **FAIL** (H1 safety, H2 value). PASS (H3 decisions) | [final-pricing-evaluation.md](../evidence/final-pricing-evaluation.md), [ADR 0031](../adr/0031-heldout-pricing-evaluation-2026-11.md) |
| LLM operator protocol (offline) | The 7 scenarios through the structured LLM protocol with a deterministic echo client: 7 / 7, 0 repairs, 0 fallbacks. **A contract test, NOT a model's skill** | PASS (integration only) | [final-agent-llm-protocol.json](../evidence/final-agent-llm-protocol.json), [ADR 0029](../adr/0029-llm-provider-for-the-ai-operator.md) |

### 1.2 Real external data

Four public sources were ingested with full provenance: Open-Meteo, NESO Carbon Intensity,
FRED and EIA. Each batch records its source, retrieval time and contract version, and goes
into a content-addressed raw archive. FRED and EIA were verified live on 2026-10-03, and
BigQuery parity was checked live in the sandbox (28 checks). In the forecast ablation, the
real signals **did not improve** the forecast (vWAPE 0.1288 vs 0.1304). That is expected,
because simulated demand does not depend on them. Sources: [phase-2.md](../evidence/phase-2.md),
[ablation](../evidence/phase-4-ablation-external-seed42.json).

### 1.3 Stripe Sandbox behaviour

All tests ran in test mode against a real Stripe Sandbox, never live mode, and never as a
load test. Phase 7 passed `make stripe-verify` 5 / 5:
- real signed webhooks go into the inbox, the state is re-fetched, and the control plane
  updates;
- the decline-then-recover path works;
- a real Test Clock renewal and cancellation work;
- the 33 real events, replayed newest-first, converge to the live state.

Sources: [phase-7.md](../evidence/phase-7.md), [sandbox report](../evidence/phase-7-stripe-sandbox.json).
Phase 16 re-ran it once on 2026-11-05: **5 / 5 again** ([phase-16.md](../evidence/phase-16.md)).

### 1.4 Measured production-like performance

| What | Measured | Source |
| --- | --- | --- |
| 100K virtual customers (local, one host) | 9.74M events, 4,494.6 ev/s, op p99 21.3 ms, final state = oracle, 0 deadlocks: **10 / 10 SLOs** | [phase-14.md](../evidence/phase-14.md) |
| M1-budget ladder (emulated 8 GB M1 on Linux: 4 GB harness, no swap, 1 GB Postgres) | **100K, 250K and 500K PASS 12/12** on the final code. 500K: 48.7M events, 4,370 ev/s, 0 OOM kills, warehouse = archive with 0 duplicate ids. In the 500K run Postgres swap was uncapped (14.5 MB swapped), so 250K is the largest step fully in budget | [final-scale.md](../evidence/final-scale.md) |
| 1M virtual customers | **NOT RUN.** Phase 14: blocked by index memory, so **the Phase 14 gate did not pass**. Final run: skipped by the owner after the redesign | same |
| Resilience (12 injected failures) | Seed 801: **10 / 12 FAIL**. After the owner-approved remediation: 12 / 12 on fresh seeds 803 and 804 | [postmortem](incident-postmortem.md) |
| GCP deployment | 93 resources on 2026-11-01 (the live loop added 13 more), everything private. Smoke 46 / 46 (runs 1-3 failed and are kept) | [deployment.md](../evidence/deployment.md) |
| Live loop (synthetic world, seed 9001) | Daily ticks since 2026-11-04. Retrains run automatically on drift and on schedule. A shadow model passed, and a promotion request is **PENDING the owner**. Tick 7 failed on a timeout and the next tick recovered. Live smoke 49 / 49 | [live-loop.md](../evidence/live-loop.md) |

### 1.5 Assumptions

The simulator stands in for reality: constant elasticity, no cross-price effects, the latent
churn mechanics described in ADR 0007. Google IAM and Stripe behave as documented. Cloud
costs are list-price estimates. See [security-assumptions.md](security-assumptions.md) and
[cost-report.md](cost-report.md).

### 1.6 Limitations

See [limitations.md](limitations.md). The ones that matter most:
- The pricing optimiser is **safe against its hard constraints but has not been shown to be
  profitable**: the fresh held-out evaluation (901-905) failed, and one world lost money.
- **1M was not run.** 500K was replayed correctly in an emulated 8 GB-M1 budget (not on an M1).
- **The AI operator has not been benchmarked with a live LLM.** The Gemini adapter, its
  validation and its fallbacks are built and tested offline. A live run needs an API key.
- Phase 15 passed only after remediation.
- Cloud spend is measured only as Google's budget-notification figure (GBP 0.65 for
  November to date, 2026-11-05). No per-service billing export is configured.
- The live loop's retrain cost is bounded by a 364-day window (ADR 0030), but the bound is
  not reached until 2027-04. The cost after that is extrapolated.

## 2. How the system is built

- [Architecture](architecture.md): the logical loop, the enforced layers, the deployed topology.
- [Data lineage](data-lineage.md): every version id, and how it is derived.
- [Decision trace](decision-trace.md): one real pricing decision, from simulator to truth
  scoring, including why it lost money.
- [Model cards](model-cards.md): forecaster, elasticity, recovery, churn / CLV.
- [Experiment cards](experiment-cards.md): the ten final experiments.

## 3. How it is operated

- [SLO report](slo-report.md)
- [Load-test report](load-test-report.md)
- [Incident postmortem](incident-postmortem.md): the Phase 15 crash collateral, and live tick 7
- [Cost report](cost-report.md)
- [Security assumptions](security-assumptions.md)
- [Reproducibility guide](reproducibility.md)

## 4. Method: how the results were kept honest

1. **Pre-registration.** Every acceptance threshold, SLO and policy was written and
   hash-pinned by a test before the run it judges.
2. **Held-out worlds are evaluated once.** Methods are developed on seed 1. A held-out seed
   is built and judged exactly once, and its result is kept even when it fails.
3. **Failures stay in the record.** Phase 6 seed 42, Phase 9 seeds 44, 45 and 202, Phase 10
   seed 601 and Phase 15 seed 801 all failed and are reported. Each later pass came from an
   owner-approved amendment, developed on dev data and judged on a fresh, pre-registered
   seed.
4. **Ground truth is out of reach.** Only `praxis.science` may join model output with truth.
   An import contract enforces this in CI.
5. **Phase 16 measured and documented; it did not tune.** No model, threshold or config was
   changed. The final acceptance suite's results, including any failure, are in
   [phase-16.md](../evidence/phase-16.md) and
   [phase-16-test-report.json](../evidence/phase-16-test-report.json).
