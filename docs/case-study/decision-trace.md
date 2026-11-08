# One decision, end to end

This trace follows one real pricing decision record through every step that produced it, and
then through the truth scoring that judged it. It comes from the **Phase 6 held-out world, seed
42**. That evaluation **FAILED**, and this decision is one of the reasons. It was chosen
because it shows the whole mechanism: data, models, constraints, record and truth scoring,
including a confident decision that turned out wrong.

Everything is **SYNTHETIC**. Amounts are simulated GBP; the records store micro-GBP. The policy
ran in **shadow mode**, so nothing executed.

Sources:
- The decision record is in `data/pricing/seed42-c1000/shadow-phase6/decisions.jsonl`. This is
  a generated output, gitignored, and rebuilt by `make pricing-data pricing-shadow`.
- The scored row is in [`phase-6-eval-seed42-shadow.json`](../evidence/phase-6-eval-seed42-shadow.json).
- The artifact manifests are [`phase-4-artifact-manifest.json`](../evidence/phase-4-artifact-manifest.json)
  and [`phase-5-artifact-manifest.json`](../evidence/phase-5-artifact-manifest.json).
- The narrative is in [`phase-6.md`](../evidence/phase-6.md).

```mermaid
sequenceDiagram
    participant S as Simulator (seed 42)
    participant W as Warehouse + dbt
    participant F as Forecast artifact
    participant E as Elasticity artifact
    participant O as Optimiser + guardrails
    participant P as Postgres decision store
    participant T as praxis.science (truth)
    S->>W: events -> raw.sim_events -> marts (panel-8b9cab7bf9c927c0)
    W->>F: panel prefix -> demand-hybrid-f4ab82a52a1c (feature date 2026-07-19, fresh)
    W->>E: randomised tests -> units-efcc3bdae7231916 -> elasticity-hier-7aeb4dc45d43
    W->>O: market-ea7a903a0ebfb5bf, prices-0b2f77a8bcb97863, churn slope, CLV
    F->>O: demand + interval width 0.363
    E->>O: tier elasticities with SD
    O->>O: 49 candidates, 48 rejected with reasons, P(improve) 0.904 >= 0.90
    O->>P: record dec-b42775bc5fdc72f57e1b (mode shadow, policy-ac62f347cbda)
    P-->>O: recorded (trigger: nothing executes without a record; shadow never executes)
    T->>T: replay the chosen price against latent customers
    T-->>P: true objective -GBP 203 / day (predicted +GBP 370 / day)
```

## 1. Inputs and lineage (from the record's `lineage` block)

| Field | Value |
| --- | --- |
| cycle | `praxis-pricing:shadow:2026-07-20`, product `api_requests`, mode `shadow` |
| forecast model | `demand-hybrid-f4ab82a52a1c` (Phase 4 held-out artifact; `data_version` `panel-8b9cab7bf9c927c0`; feature version `demand_features.v1`; feature date 2026-07-19, freshness `fresh`) |
| elasticity model | `elasticity-hier-7aeb4dc45d43` (Phase 5 held-out artifact; data `units-efcc3bdae7231916`) |
| market snapshot / price plan | `market-ea7a903a0ebfb5bf` / `prices-0b2f77a8bcb97863` |
| policy | `policy-ac62f347cbda` (hash of the pinned `configs/pricing/policy.toml`) |
| elasticity used | enterprise -0.674 (SD 0.033), growth -1.210 (0.027), starter -1.875 (0.032) |
| churn evidence | slope 0.00166, SD 0.0161 (from experiment day 2026-02-02), 697 exposed customers, 28-day window |
| forecast interval relative width | 0.363 (limit 1.0) |

## 2. Optimisation and guardrails

- Current price 0.368 GBP per unit (368,000 micros). Anchor 0.400.
- 49 candidate prices were scored. Each candidate has a mean, SD, p05 and p95 of the change in
  the objective, and a probability of improvement.
- 48 candidates were rejected, and each rejection names its reason (for example `capacity`).
- Chosen: **0.3864 GBP (+5.0 %)**, limited by `max_step` (5 % per cycle).
- Predicted change in the objective: **+GBP 369.81 / day**. SD 274.08, p05 -74.49, p95
  +814.16. P(improve) **0.904**, just above the 0.90 confidence rule.
- Hard guardrails passed: price floor and ceiling, max step, cooldown (last change
  2026-04-25), extrapolation 0.035 (limit 0.182), margin floor (net margin 0.653, limit
  0.20), capacity, and the churn guardrail (projected extra churn 0.00137, limit 0.005).
- The naive baseline is the unconstrained point optimum. It would have tripled the price to
  1.104 GBP and broken four constraints.

## 3. Record and audit

The decision was persisted before anything else could act on it. The record holds the full
inputs, every candidate, every rejected alternative, the guardrail results and the lineage.

A Postgres trigger refuses an execution that has no record, a wrong price, shadow mode, or a
recommendation that was never approved. `execute` also reads the record back and verifies
its checksum first, so a tampered record cannot execute.

Re-optimising the record's own inputs reproduces it byte for byte, which is true for all 40
records of this world ([phase-6.md](../evidence/phase-6.md), "Performance").

## 4. Scored against truth (after the run; only `praxis.science` can read truth)

| Quantity per day | Predicted | True |
| --- | --- | --- |
| contribution change | +GBP 398.81 | +GBP 356.59 |
| objective change (contribution - churn value) | +GBP 369.81 | **-GBP 203.13** |
| extra churn per exposed customer (28 days) | 0.00137 (projected upper) | 0.00166 |

The contribution model was close: the forecast plus the causal elasticity predicted the
revenue effect within 12 % for this decision, and within 4 % pooled over all 17 changes.

The loss came from churn. The churn slope (0.0017 ± 0.016) came from a February experiment
window where the voluntary-churn hazard was low. In the July shadow window, utilisation was
about 0.95 and the true slope was 0.031-0.052. Phase 6 calls this **transport bias**. It
explains why all 8 `api_requests` changes lost value, and the losses grew each cycle.

## 5. What happened next

- The seed-42 gate failed (`no_harm` -GBP 3,526 / day, `direction` 4 / 17) and was not re-run.
- The method was amended on the development world only: a relative churn effect, scaled by
  the churn rate observed at decision time. It was then evaluated once on seed 43, which was
  pre-registered beforehand. Result: PASS, all holds.
- In Phase 9, the long-horizon guardrails replayed this same seed-42 world as a controlled
  example. They altered all 17 Phase 6 changes, including all 13 losing ones, so no price
  moved ([phase-9.md](../evidence/phase-9.md)).

## 6. The operations trace (the other half of "end to end")

The ops path is traced by two tests:
- `tests/ops/test_trace_e2e.py`: one synthetic event, one trace id, across publish, consume,
  model inference, optimiser and persistence.
- The Playwright journey `make e2e`: alert, incident, trace, pending request, then an
  authorized approval in the console.

Results are in [phase-11.md](../evidence/phase-11.md), [phase-13.md](../evidence/phase-13.md)
and the Phase 16 run in [phase-16.md](../evidence/phase-16.md).
