# Constrained pricing optimiser (Phase 6; churn and CLV inputs replaced in Phase 9)

**All data is SYNTHETIC** (Phase 1 simulator). Decision: ADR 0013 (objective, constraints,
audit, shadow evaluation, and the pre-evaluation amendment) and ADR 0016 (Phase 9: churn
hazard + CLV model, segment valuation, long-horizon guardrails). Evidence:
`docs/evidence/phase-6.md`, `docs/evidence/phase-9.md`, and `docs/customer-value.md`.

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
churn cost   = response * sum_g CLV_g * E_g * log(p / p0) / 28      g = tier in force (Phase 9)
objective    = contribution - churn cost;   decide on  E[delta] - 0.5 * SD[delta]
```

* **Causal response only.** The forecast is used at the current price; its price features are
  predictive and never price a change. Elasticities come from randomised tests (Phase 5).
* **Uncertainty.** Tier elasticities move together (one shock, comonotone) over a 199-point
  equal-probability grid; the churn slope has its own 21-point grid. Expectations, SD,
  quantiles and P(improvement) are deterministic.
* **Churn response (Phase 9).** Price multiplies the voluntary churn hazard. The evidence is the
  bill elasticity of churn, gamma_p, from all concurrent randomised tests jointly
  (`praxis.elasticity.churn`), partially pooled across products. At decision time it applies to
  each customer: extra churn = q_i x gamma_p x s_ip x log(p / p0), with q_i the customer's
  calibrated baseline 28-day voluntary churn (Phase 9 hazard model x recent calibration) and
  s_ip the product's share of its bill. E_g = sum over segment g of q_i s_ip. The posterior and
  the calibration's uncertainty enter the decision. (Phase 6 used per-test relative slopes
  scaled by the observed churn rate: ADR 0013, second amendment.)
* **Robust churn class (ADR 0016 second and third amendments).** The decision must be
  confident under EVERY posterior of the class (`evidence.churn_models`, maxmin): full-Bayes
  hierarchical pooling across products, complete pooling, no pooling, and `meta_analytic`:
  the product's own estimate under a robust meta-analytic-predictive prior built from the
  estimates of earlier churn experiments (`configs/pricing/churn_archive.json`, development
  experiment worlds only; `make churn-archive`, checksum pinned in the policy). One test
  that happens to read "no churn effect" can no longer carry a raise on its own.
* **CLV (Phase 9).** Each extra churner is valued at its segment's CLV from the CLV model
  (`praxis.customer`): the lifetime value of the customers the price would lose. Phase 6 used
  one average-CLV proxy for everyone.

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
| segment churn guardrail (Phase 9) | the same per segment: starter 0.8 pp, growth 0.5 pp, enterprise 0.3 pp (segments with >= 20 exposed customers) |
| churn-risk ceiling (Phase 9) | no raise may take a segment's projected voluntary 28-day churn above 10% |
| price-change frequency (Phase 9) | no exposed customer may see more than 3 dates with a price change in 90 days (this one included) |
| bill share (Phase 9 amendment) | freeze a product below 5% of the experiment customers' bills: its churn response is unidentified |

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
make pricing-dev                       # seed-1 forecast, elasticity, churn response, customer model, shadow
make pricing-data customer-train pricing-shadow PX_SEED=1   # dev shadow world only
make pricing-world SEED=301 && make pricing-shadow PX_SEED=301   # Phase 9 held-out (301-303; run once)
make churn-archive                     # rebuild the MAP archive (development worlds only)
.venv/bin/python -m praxis.pricing decide --db WH --forecast-model DIR \
    --elasticity-model data/models/elasticity --elasticity-report REPORT \
    --churn-response CHURN.json --customer-model DIR --as-of 2026-07-20
.venv/bin/python -m praxis.pricing {approve|execute|show} --database-url URL --decision-id ID
```

## Shadow evaluation

The shadow world is the Phase 4 forecast world continued for 8 weekly cycles
(`pricing_shadow.toml`; `arrival_horizon_days` keeps its first 196 days identical, and the
evaluation refuses a forecast artifact not trained on exactly that prefix). Truth comes from
the simulator's own equations through a read-only observer: every candidate price of every
decision is scored on its 7 days with the world's actual customers and service state, valued
with the decision's own payment-loss rate and CLV (Phase 9: true extra churners counted per
tier and valued at the CLV the decision recorded for that tier). Gated (`shadow_acceptance.toml`): safety
(constraint compliance, no execution, complete records, lineage, churn guardrail in truth,
five stress cases), no harm, direction of changes, calibration. Reported: change share,
captured share of the best feasible candidate, a naive point-estimate optimum without
constraints.

## Known failure modes (measured)

* **Churn evidence.** The Phase 5 tests measure the churn response to price to about +-0.016
  per unit log price (pooled), about the size of the effect. The held-out world's estimate
  (0.0017) was low by ~1.3 SD.
* **Single-test churn evidence (fixed by ADR 0016 third amendment).** Even per unit log bill,
  one 28-day test measures the response to +-0.85-0.95 (pooled) around ~1.7. With a flat prior,
  raises cleared P >= 0.90 mainly in worlds whose test read low (optimizer's curse); held-out
  seed 202 (pooled 0.05) lost GBP 49 / day. The meta-analytic member of the robust class blocks
  this: a raise must also be confident under the archive of earlier experiments.
* **Churn transport (fixed by the second amendment).** Demand growth raises utilisation
  (0.59 -> 0.96 over the year) and degraded service raises the base hazard (0.00115 ->
  0.002-0.003 per day), so the ABSOLUTE churn slope measured in February understated August's
  by 1.5-2.3x. With that absolute slope the first held-out run (seed 42) made 17 changes, 13 of
  which lost value in truth. The relative model scales the evidence by the current churn rate.
* Contribution predictions are accurate (predicted / true 1.04 held-out, 0.98 dev).
* Cross-price effects are assumed absent (true in this simulator).
