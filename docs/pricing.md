# Constrained pricing optimiser (Phase 6)

**All data is SYNTHETIC** (Phase 1 simulator). Decision: ADR 0013 (objective, constraints,
audit, shadow evaluation, and the pre-evaluation amendment). Evidence: `docs/evidence/phase-6.md`.

```text
Phase 4 forecast service (current prices)  Phase 5 evidence (tier elasticities, churn tests)
              \                                    /
               praxis.pricing.inputs  <-  marts (cost, utilisation, served share, payment
               (PricingProblem per product)         loss, customers, list prices)  + price book
                        |
               praxis.pricing.optimiser   gates -> candidates -> objective -> constraints
                        |                 -> refine -> hold / freeze / change
               praxis.pricing.decision    content-addressed audit record
                        |
               praxis.pricing.store       Postgres: pricing_decisions (append-only),
                        |                 pricing_approvals, price_executions (trigger-guarded)
               praxis.pricing.service     one cycle: every product decided and recorded
                        |
               praxis.science.pricing_shadow   shadow cycles scored against simulator truth
```

## What is decided

One **list price per product** per weekly cycle (the business sets one list price per
product). Every product gets exactly one recorded decision per cycle, whatever happens:

| Status | Meaning | Carries a price |
| --- | --- | --- |
| `change` | a feasible price, confidently better than the current one | yes |
| `hold` | the current price is the best feasible option (or cooldown) | no |
| `frozen` | evidence too weak or too uncertain to move the price | no |
| `infeasible` | no candidate satisfies every constraint | no |
| `unavailable` | inputs missing, stale, invalid, or the objective is not finite | no |

## Objective (per day, micro-GBP)

```text
D_s(p)       = forecast_s(at current price) * (p / p0) ** e_tier        segment s = region x tier
contribution = sum_s D_s(p) * served_share_s * (p * (1 - payment_loss) - marginal_cost_region)
churn cost   = exposed customers * CLV * churn_slope * log(p / p0) / 28
objective    = contribution - churn cost;   decide on  E[delta] - 0.5 * SD[delta]
```

* **Causal response only.** The forecast is used at the current price; its price features are
  predictive and never price a change. Elasticities come from randomised tests (Phase 5).
* **Uncertainty.** Tier elasticities move together (one shock, comonotone) over a 199-point
  equal-probability grid; the churn slope has its own 21-point grid. Expectations, SD,
  quantiles and P(improvement) are deterministic.
* **Churn slope.** Price multiplies the churn hazard, so the evidence is RELATIVE: the log ratio
  of treatment and control churn rates per unit log price, from each product's randomised test,
  partially pooled across products (empirical Bayes). At decision time it is scaled by the
  28-day churn probability observed in the marts just before the decision, so it follows the
  current churn regime (ADR 0013, second amendment). Its posterior uncertainty enters the
  decision.
* **CLV** is a labelled proxy (daily contribution per active customer x min(1 / churn hazard,
  365 days)) until Phase 9.

## Constraints (`configs/pricing/policy.toml`, hash = `policy_version`)

| Constraint | Rule |
| --- | --- |
| price floor / ceiling | absolute, per product |
| max step | x(1 + 0.05) or /(1 + 0.05) per cycle |
| cooldown | no change within 14 days of the last list-price change (marts or price book) |
| extrapolation | abs(log(p / price when tested)) <= 0.1823, the tested range |
| margin floor | (net price - marginal cost) / net price >= 0.20 in every region served |
| capacity | projected utilisation (forecast q90, most demand-increasing elasticity) <= 0.85, unless the change lowers load |
| churn guardrail | extra churn probability per exposed customer over 28 days <= 0.5 pp, at the one-sided 95% bound; raises only |

Gates that freeze before optimising: no randomised evidence for the product, evidence older
than 365 days, a material tier (> 5% of demand) with non-negative elasticity or posterior SD
> 0.25, forecast 80% band wider than the point forecast. After optimising: P(improvement) < 0.90.
Only a FRESH forecast (features 1 day old) may move a price; the stale fallback cannot.

Every constraint is checked on the exact integer price; a returned price satisfies all of
them (Hypothesis property tests against an independent scalar reference).

## Audit, modes, execution

* The record (`Decision.record()`) holds input model versions, forecast feature date, market
  snapshot hash, price plan version, every candidate with its objective terms and violated
  constraints, constraint limits, chosen price, rejected alternatives with reasons, reason
  codes, guardrail outcome, prediction, and the policy version. Its id is a content hash, so a
  retried cycle is a no-op; a second, different decision for the same cycle and product is a
  conflict, never an overwrite.
* **shadow** (default): record only. **recommend**: executable after a named approval.
  **execute**: needs `allow_execute = true` in the policy.
* Postgres (migration 0002) refuses, by trigger, any execution without a matching CHANGE
  record, at another price, in shadow mode, or unapproved in recommend mode; the audit tables
  are append-only; execution re-verifies the record's checksum.
* The cycle has no LLM dependency (ADR 0004).

## Running

```bash
make pricing-dev                       # seed-1 forecast artifact + elasticity + shadow (dev)
make pricing-data pricing-shadow PX_SEED=1   # dev shadow world only (forecast/elasticity done)
make pricing-data pricing-shadow PX_SEED=43  # held-out (42 and 43 are spent: see the evidence)
.venv/bin/python -m praxis.pricing decide --db WH --forecast-model DIR \
    --elasticity-model data/models/elasticity --elasticity-report REPORT --as-of 2026-07-20
.venv/bin/python -m praxis.pricing {approve|execute|show} --database-url URL --decision-id ID
```

## Shadow evaluation

The shadow world is the Phase 4 forecast world continued for 8 weekly cycles
(`pricing_shadow.toml`; `arrival_horizon_days` keeps its first 196 days identical, and the
evaluation refuses a forecast artifact not trained on exactly that prefix). Truth comes from
the simulator's own equations through a read-only observer: every candidate price of every
decision is scored on its 7 days with the world's actual customers and service state, valued
with the decision's own payment-loss rate and CLV. Gated (`shadow_acceptance.toml`): safety
(constraint compliance, no execution, complete records, lineage, churn guardrail in truth,
five stress cases), no harm, direction of changes, calibration. Reported: change share,
captured share of the best feasible candidate, a naive point-estimate optimum without
constraints.

## Known failure modes (measured)

* **Churn evidence.** The Phase 5 tests measure the churn response to price to about +-0.016
  per unit log price (pooled), about the size of the effect. The held-out world's estimate
  (0.0017) was low by ~1.3 SD.
* **Churn transport (fixed by the second amendment).** Demand growth raises utilisation
  (0.59 -> 0.96 over the year) and degraded service raises the base hazard (0.00115 ->
  0.002-0.003 per day), so the ABSOLUTE churn slope measured in February understated August's
  by 1.5-2.3x. With that absolute slope the first held-out run (seed 42) made 17 changes, 13 of
  which lost value in truth. The relative model scales the evidence by the current churn rate.
* Contribution predictions are accurate (predicted / true 1.04 held-out, 0.98 dev).
* Cross-price effects are assumed absent (true in this simulator).
