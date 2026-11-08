# Limitations

Read this page before quoting any result. Each item links to the evidence that records it.

## 1. What the results are, and are not

- **Everything decision-related is simulated.** Customers, demand, prices, revenue, churn,
  payments, recoveries and uplift all come from the Praxis simulator. **None of it is a
  real-world business result.** Amounts in "GBP" are simulated units.
- The simulator is the ground truth. Phase 1 deliberately built in difficult cohorts, noise,
  spikes and transport effects, but the real world has structure the simulator does not:
  for example cross-price effects, competitors, and non-constant elasticity.
- **Real external data** (Open-Meteo, NESO, FRED, EIA) was ingested and verified live once
  (2026-10-03). It gave **no forecast lift**, which is correct, because simulated demand does
  not depend on it ([phase-4.md](../evidence/phase-4.md)). It shows the ingestion engineering
  works; it is not a modelling result.
- **Stripe** was exercised only in **Sandbox (test mode)**, with 4 Test-Clock customers per
  verification run. That is representative integration, not scale or live payments.

## 2. Model and decision limitations

| Limitation | Evidence |
| --- | --- |
| **The pricing optimiser has not moved a price on any held-out world that passed.** Every held-out pass (seeds 43 and 301-303) is holds and freezes. Value-adding moves appear only on development worlds | [phase-6.md](../evidence/phase-6.md), [phase-9.md](../evidence/phase-9.md) |
| Held-out pricing FAILS stand: Phase 6 seed 42, Phase 9 seeds 44, 45 and 202 | same |
| The Phase 6 and Phase 9 shadow results were measured with a defective forecast loader (fixed in Phase 10). **The fresh pre-registered evaluation on the fixed code (seeds 901-905) FAILED**: H1 safety (901 direction 0.75; 905 lost 210.9 GBP/day from under-predicted churn on `data_transfer_gb` raises) and H2 value (95 % CI [-113.9, +203.9]). Not fit for live pricing | [final-pricing-evaluation.md](../evidence/final-pricing-evaluation.md) |
| Spike days cannot be forecast (80 % coverage 0.27) | [phase-4.md](../evidence/phase-4.md) |
| Elasticity cell-level recovery did not replicate on seed 43 | [phase-6.md](../evidence/phase-6.md) |
| The recovery gate passed only after an owner-approved amendment. The original ECE gate would have failed (0.034 vs 0.03). The dev world has a known truth-error failure | [phase-8.md](../evidence/phase-8.md) |
| The churn bill-share model understated gpu's response about 2.7x. Absence of evidence was read as evidence of absence (seed 202) | [phase-9.md](../evidence/phase-9.md) |
| MLOps attempt 1 FAILED on held-out seed 601. Attempt 2 passed with 1 day of detection slack, on a steady world without organic growth | [phase-10.md](../evidence/phase-10.md) |
| **The agent benchmark scores the deterministic triage investigator, NOT an LLM.** A Gemini adapter exists (ADR 0029) and passes the protocol offline (echo client). **No live model has been run** (no API key on this machine) | [phase-12.md](../evidence/phase-12.md), [final-agent-llm-protocol.json](../evidence/final-agent-llm-protocol.json) |

## 3. Scale and performance limitations

- **The 1M milestone was NOT RUN.** In Phase 14 it was blocked by the DuckDB index, so the
  **Phase 14 gate did not pass**. After the warehouse redesign (ADR 0028) the owner chose to
  skip it. Nothing above 500K is claimed.
- **The M1-budget ladder is an emulation on Linux, not an M1.** 100K, 250K and 500K replay
  correctly in a 4 GB no-swap cgroup. In the 500K run, Postgres swap was uncapped after a host
  reboot (14.5 MB swapped), so 250K is the largest step fully inside the registered budget
  ([final-scale.md](../evidence/final-scale.md)).
- All scale runs use one host and an in-memory broker. They do not measure Pub/Sub, Cloud Run
  or BigQuery at scale.
- Each scale configuration ran once, with no repeats.
- The 1K chaos benchmark is about 14 % below Phase 16 (1,541.9 vs 1,800.9 ev/s), still above
  the floor. The first run after ADR 0028 failed (575 ev/s) until amendment 1.

## 4. Resilience limitations

- **Phase 15 passed only after an owner-approved remediation. The earlier FAILs stand:**
  seed 801 scored 10 / 12, `feature_stale` and `consumer_crash` failed, and the
  `forecast_freshness` SLO was tightened from 48 h to 24 h.
- Crashes are in-process exceptions, not OS process kills. Pub/Sub was the emulator or the
  in-memory broker.
- Terraform destroy and recreate is proven with **mocked** providers only. The one real
  teardown (`make cloud-destroy`) has never run.
- One runbook (DLQ redrive) is exercised end to end. The others are validated for structure
  and their alerts are tested.

## 5. Operations and cloud limitations

- The deployment is a development environment: a single Postgres VM with no HA, services
  that scale to zero, and no always-on SLO evaluation. The ops monitor runs once a day.
- **The live loop is an operating environment, never gate evidence.** It runs the synthetic
  world, seed 9001.
- Its retrain cost grew roughly with the square of the history. It is now bounded by a sliding
  364-day window (ADR 0030), which first binds around 2027-04-05. The cost at that bound is
  extrapolated, not measured.
- **Automatic rollback has not happened in the cloud**, because it needs a promotion first.
  A PENDING promotion request (`demand-hybrid-84d53ff98b99`) is waiting for the owner.
- Cloud cost is measured only as Google's budget-notification figure: GBP 0.65 for November
  to date on 2026-11-05 ([final-cost-verification.md](../evidence/final-cost-verification.md)).
  There is no per-service breakdown.
- There is no cloud fresh-feature pipeline for the original deployment world. The live loop
  provides the fresh warehouse for its own world.
- Console access through `gcloud run services proxy` is verified for reads and approver
  identification ([final-console-proxy.md](../evidence/final-console-proxy.md)). No approval
  was made through it. The dunning subscription has no cloud consumer role.
- The deployed approver token has no expiry. The console code built after 2026-11-05
  refuses such a token outside `local` (ADR 0021 amendment 1), so the secret must be rotated
  before the next deploy.

## 6. Process limitations

- Several gates passed only after owner-approved amendments (Phases 4, 6, 8, 9, 10 and 15).
  Each amendment was made on development data, pre-registered, and judged once on a fresh
  seed. The earlier failures are kept in the evidence. Amendments improve methods, but they
  also mean the final configuration was chosen after earlier held-out failures were seen.
  The held-out seeds of each attempt are independent; the sequence of attempts is not.
- Single held-out worlds per attempt (or three, in Phase 9), at 1K-8K customers. The error
  bars on "the method generalises" are wide.
