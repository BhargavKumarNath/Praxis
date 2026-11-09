# Results

This page collects what Praxis has measured, including what failed. Every number links to
the evidence file it came from.

All customers, demand, revenue, churn, payments and uplift are synthetic, produced by the
Praxis simulator. "GBP" amounts in experiments are simulated units, not money anyone earned.

## How results were kept honest

1. **Pre-registration.** Every acceptance threshold, SLO and policy was written down and
   pinned by a hash test before the run that judges it.
2. **Held-out worlds are evaluated once.** Methods are developed on seed 1. A held-out seed is
   built and judged once, and its result is kept even when it fails.
3. **Failures stay on record.** When a gate failed, the fix was developed on development data,
   registered, and judged on a fresh seed. The failed run's evidence is kept beside the pass.
4. **Ground truth is out of reach.** Only `praxis.science` may join model output with truth,
   and an import contract enforces it in CI.

One consequence is worth stating plainly: several gates passed only on a second attempt.
Each held-out seed is independent, but the sequence of attempts is not, so a later pass was
chosen after an earlier failure had been seen.

## Experiments on synthetic worlds

| Experiment | Held-out result (run once) | Verdict |
| --- | --- | --- |
| Demand forecast vs seasonal naive | vWAPE 0.1288 vs 0.1661 (ratio 0.776, CI of the difference [-0.0455, -0.0292]), seed 42 | PASS 11 / 11 |
| Interval calibration | 50 / 80 / 90 % coverage 0.493 / 0.795 / 0.897; spike days only 0.272 | PASS |
| Elasticity recovery | Pooled -1.3335 vs truth -1.3315 (0.15 %), 14 / 14 segment orderings, seed 42 | PASS 8 / 8 (cell-level checks did not replicate on seed 43) |
| Payment recovery vs fixed retry | +GBP 47.72 per episode, paired 95 % CI [+21.98, +75.99]; recovery 61.1 % vs 54.6 % | PASS 14 / 14, after an owner-approved amendment |
| CLV / churn guardrails | Replayed the failed Phase 6 world: altered 17 / 17 changes, removed all 13 losing ones | PASS (a controlled example, not a fresh test) |
| Pricing optimiser vs static | Fresh evaluation, seeds 901-905: mean +GBP 34.3 / day, 95 % CI [-113.9, +203.9]; seed 905 lost GBP 210.9 / day; 0 constraint violations | **FAIL** |
| Model lifecycle drill | Seed 601 failed (1 / 12 checks). Seed 621: 12 / 12, including a forced bad champion restored | PASS on attempt 2 |
| Agent incident benchmark | 7 / 7, 0 prohibited executions, with the deterministic triage investigator | PASS |
| Live LLM operator | Claude Opus 5.5 on ten new held-out scenarios: 10 / 10, 0 prohibited executions | PASS on attempt 5 |

### Forecasting

The hybrid model (ridge mean plus calibrated LightGBM quantiles) beat every baseline out of
time: 0.1288 against seasonal naive 0.1661, seasonal moving average 0.1401 and plain LightGBM
0.1357, over 8,064 forecasts. It does no better than naive on unannounced spike days, and real
weather and grid signals gave no lift (0.1288 vs 0.1304). That last one is expected, because
simulated demand does not depend on them.
[phase-4.md](evidence/phase-4.md), [backtest](evidence/phase-4-backtest-eval-seed42.json)

### Elasticity

Randomised price tests plus a hierarchical Bayesian model recovered the simulator's latent
elasticities on 8,000 customers. A contamination world (20 % of controls charged the
treatment price) was detected at 0.200 / 0.189, and the IV estimate landed within 0.5 % of
truth while the artifact was correctly refused. On a fresh replication world the tier and
pooled estimates held, but two cell-level checks failed.
[phase-5.md](evidence/phase-5.md), [seed 42](evidence/phase-5-eval-seed42-recovery.json),
[seed 43](evidence/phase-6-seed43-elasticity-recovery.json)

### Pricing

This is the result that did not work out. The optimiser has never broken a constraint, and
the unconstrained alternative (roughly tripling prices) would have lost GBP 287k to 1.06M a
day in truth. But it has not been shown to beat static prices on a held-out world.

- Phase 6 seed 42 failed: 17 changes, 4 in the right direction, -GBP 3,526 / day.
- Phase 9 failed on seeds 44, 45 and 202, then passed on 301-303, but every held-out pass
  consisted of holds and freezes: no held-out price moved.
- The fresh evaluation on fixed code (ADR 0031, seeds 901-905) made 15 changes, 9 in the right
  direction. Seed 905 lost GBP 210.9 / day because churn on `data_transfer_gb` raises was
  under-predicted.

[final-pricing-evaluation.md](evidence/final-pricing-evaluation.md),
[phase-6.md](evidence/phase-6.md), [phase-9.md](evidence/phase-9.md)

**One decision, traced.** In the Phase 6 seed-42 world, the optimiser raised `api_requests` by
5 % (the per-cycle cap). It scored 49 candidates, rejected 48 with named reasons, and chose a
price with P(improvement) 0.904, just above the 0.90 rule. The contribution model was close
(+GBP 399 predicted, +GBP 357 true), but the objective went from +GBP 370 predicted to
-GBP 203 true. The churn slope had been measured in February, when utilisation and churn were
low. By July, utilisation was about 0.95 and the true slope was 20 to 30 times larger. That
"transport bias" is why the churn model now scales its evidence by the current churn rate and
why the long-horizon guardrails exist.
[phase-6-eval-seed42-shadow.json](evidence/phase-6-eval-seed42-shadow.json)

### Payment recovery

A mixture-cure survival model plans up to two retries (three charges in all) by expected value.
Against the fixed schedule (retry after 3 days, then 7, then suspend) on 445 held-out episodes, it recovered more
(61.1 % vs 54.6 %), made fewer failed retries (0.84 vs 1.22 per episode) and restricted
customers for fewer days (2.1 vs 5.3), at the cost of slower recovery (9.8 vs 6.9 days). The
gate passed after an owner-approved amendment to the calibration check; the raw ECE of 0.034
would have failed the original 0.03 limit.
[phase-8.md](evidence/phase-8.md), [evaluation](evidence/phase-8-eval-seed42-evaluation.json)

### MLOps lifecycle

The drill checks that a bad candidate is rejected, drift is detected before a deadline, and a
degraded champion is rolled back. Attempt 1 (seed 601) failed because the steady world raised
30 alarm days against a limit of 0. Attempt 2 (seed 621) passed 12 / 12, with one day of slack
on drift detection and a steady world without organic growth.
[phase-10.md](evidence/phase-10.md)

### AI operator

The incident benchmark has scored the deterministic triage investigator at 7 / 7 since
Phase 12, with 28 prohibited attempts refused in adversarial runs. The live LLM took five
attempts, all recorded in [ADR 0029](adr/0029-llm-provider-for-the-ai-operator.md):

| Attempt | Model | Result | Cause |
| --- | --- | --- | --- |
| 1-3 | Gemini Flash-Lite / 3.8 Flash | 0 / 7 | A schema bug, an under-investigating model, free-tier quota |
| 4 | Claude Opus 5.5 | 2 / 7 | The prompt never stated the operating procedure being scored |
| 5 | Claude Opus 5.5 | 10 / 10 | The playbook in the prompt, scored on ten new pre-registered scenarios |

Attempt 5 ran once. It took the right action in every scenario, including the ones where the
right action is none, and ignored a prompt injection planted in each task. What it shows is
that the model follows a stated procedure on faults it has not seen; it does not show
open-ended diagnosis without one.
[attempt 5](evidence/final-agent-llm-live-attempt5-heldout.json)

## Real external data

Open-Meteo, NESO Carbon Intensity, FRED and EIA are ingested with full provenance into a
content-addressed archive. FRED and EIA were verified live, and BigQuery parity was checked in
the sandbox (28 checks). [phase-2.md](evidence/phase-2.md)

## Stripe Sandbox

`make stripe-verify` passed 5 / 5 in test mode: signed webhooks into the inbox, the
decline-then-recover path, a Test Clock renewal and cancellation, and 33 real events replayed
newest-first converging to the live state. It runs four customers per verification and is
never a load test. [phase-7.md](evidence/phase-7.md)

## Performance and scale

| What | Measured | |
| --- | --- | --- |
| 100K virtual customers, one host | 9.74M events at 4,494.6 ev/s, op p99 21.3 ms, final state equal to the oracle, 0 deadlocks | 10 / 10 SLOs |
| Emulated 8 GB budget (4 GB harness, no swap, 1 GB Postgres) | 100K, 250K and 500K pass 12 / 12. 500K: 48.7M events at 4,370 ev/s, 0 OOM kills | 250K is the largest step fully in budget (Postgres swapped 14.5 MB at 500K) |
| 1M virtual customers | Not run. The original blocker (index memory) was removed by ADR 0028, but the step itself was skipped | No claim |

These runs use one Linux host and an in-memory broker. The budget run emulates an 8 GB machine
with cgroups; it is not a measurement on Apple hardware, and none of it measures Pub/Sub or
BigQuery at scale. [phase-14.md](evidence/phase-14.md), [final-scale.md](evidence/final-scale.md)

## Resilience

Twelve failures are injected against real boundaries (database loss, poison messages,
consumer crashes, stale features, a corrupt artifact and more) and judged against
pre-registered criteria. The first run (seed 801) scored 10 / 12. The final code scored
12 / 12 on fresh seeds 803 and 804.

**The incident behind the failure.** Isolated consumer crashes at 1 % per message
dead-lettered 1,305 healthy messages. Pub/Sub charges a delivery attempt to every message in a
lease, and the warehouse consumer leased whole batches, so one crash pushed its innocent
batch-mates toward the dead-letter queue. The runbook redrive restored state exactly, but the
system was not isolating failures. The fix (ADR 0025) leases with slow start, processes first
deliveries and redeliveries in separate groups, and processes any message redelivered twice
on its own. On seed 804, 217 crashes produced 0 dead letters, and a poison storm dead-lettered
exactly the 12 poison events. The first version of the fix cut throughput by 79 %, which the
nightly performance gate caught before it shipped.
[phase-15.md](evidence/phase-15.md), [phase-15-remediation.md](evidence/phase-15-remediation.md)

## Deployment

The first GCP deployment had 93 resources; with the live loop and the public read-only console
the state holds 110 (2026-11-09). The cloud smoke
test passed 46 / 46 on the first deployment and 50 / 50 after the 2026-11-08 redeploy; the live
smoke passed 49 / 49. Every earlier failed run is kept. The live loop has ticked daily since
2026-11-04. One tick timed out during a retrain, and the next tick finished the same days
without repeating the work. Its first passed shadow version was promoted by the owner from the
console on 2026-11-08.
[deployment.md](evidence/deployment.md), [live-loop.md](evidence/live-loop.md),
[final-release.md](evidence/final-release.md)

Cloud spend is estimated at GBP 0.10-0.20 a month for the deployment plus a few USD for the
live loop. The only measured figure is Google's budget notification: GBP 0.65 for November to
date on 2026-11-05. There is no per-service billing export.
[final-cost-verification.md](evidence/final-cost-verification.md)

## Limitations

- **The simulator is the ground truth.** It has deliberately difficult cohorts and noise, but
  constant elasticity, no cross-price effects and no competitors.
- **Pricing is safe, not shown to be profitable.** Not fit for live pricing.
- **Scale stops at 500K, measured on one host.** 1M was never run.
- **Several gates passed on a second attempt** (Phases 4, 6, 8, 9, 10 and 15), each after a
  registered amendment.
- **Resilience crashes are in-process exceptions**, not OS process kills, and Terraform
  destroy and recreate is proven only against mocked providers.
- **The deployment is a dev environment**: one Postgres VM without HA, services that scale to
  zero, and an ops monitor that ticks once a day.
- **The live LLM result** covers procedure execution on ten scenarios, not unscripted
  diagnosis.
