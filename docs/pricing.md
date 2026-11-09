# Pricing

The optimiser proposes one list price per product per weekly cycle, inside hard constraints,
and holds or freezes whenever its evidence is too weak to justify a move. Every product gets
exactly one recorded decision per cycle, and nothing can execute without that record. It runs
in shadow mode by default. Decisions: ADR 0013 (objective, constraints, audit) and ADR 0016
(churn, CLV and long-horizon guardrails). Results, including the failed held-out evaluation:
[results.md](results.md#pricing).

```text
forecast service (at current prices)     elasticity + churn evidence (randomised tests)
              \                                 /
          praxis.pricing.inputs  <-  marts: cost, utilisation, served share, payment loss,
          (one PricingProblem per product)    customers, list prices; customer value model
                      |
          praxis.pricing.optimiser   gates -> candidates -> objective -> constraints -> decide
                      |
          praxis.pricing.decision    content-addressed decision record
                      |
          praxis.pricing.store       Postgres: append-only decisions, approvals, executions
                      |
          praxis.science.pricing_shadow   scores shadow cycles against simulator truth
```

## Decisions

| Status | Meaning | Carries a price |
| --- | --- | --- |
| `change` | A feasible price, confidently better than the current one | yes |
| `hold` | The current price is the best feasible option, or a cooldown applies | no |
| `frozen` | Evidence too weak or too uncertain to move the price | no |
| `infeasible` | No candidate satisfies every constraint | no |
| `unavailable` | Inputs missing, stale or invalid, or the objective is not finite | no |

## Objective (per day, micro-GBP)

```text
D_s(p)       = forecast_s(at current price) * (p / p0) ** e_tier        segment s = region x tier
contribution = sum_s D_s(p) * served_share_s * (p * (1 - payment_loss) - marginal_cost_region)
churn cost   = response * sum_g CLV_g * E_g * log(p / p0) / 28      g = tier in force
objective    = contribution - churn cost;   decide on  E[delta] - 0.5 * SD[delta]
```

- **Causal response only.** The forecast is used at the current price; its own price features
  never price a change. Elasticities come from the randomised tests.
- **Uncertainty.** Tier elasticities move together over a 199-point equal-probability grid,
  and the churn slope has its own 21-point grid, so expectations, quantiles and
  P(improvement) are deterministic.
- **Churn.** Price multiplies the voluntary churn hazard. The evidence is the bill elasticity
  of churn from all concurrent tests, partially pooled across products. For each customer,
  extra churn = q_i x gamma_p x s_ip x log(p / p0), where q_i is their calibrated baseline
  28-day churn and s_ip the product's share of their bill.
- **Robust to a lucky test.** The decision must be confident under every posterior in a class:
  hierarchical pooling, complete pooling, no pooling, and a meta-analytic prior built from
  earlier development experiments (`configs/pricing/churn_archive.json`, checksum-pinned). One
  test that happens to read "no churn effect" cannot carry a raise on its own.
- **Value of a lost customer.** Each extra churner is valued at its segment's CLV from the
  customer model below.

## Constraints (`configs/pricing/policy.toml`, hashed into `policy_version`)

| Constraint | Rule |
| --- | --- |
| Price floor / ceiling | Absolute, per product |
| Max step | x(1 + 0.05) or /(1 + 0.05) per cycle |
| Cooldown | No change within 14 days of the last list-price change |
| Extrapolation | abs(log(p / price when tested)) <= 0.1823, the tested range |
| Margin floor | (net price - marginal cost) / net price >= 0.20 in every region served |
| Capacity | Projected utilisation (forecast q90, most demand-increasing elasticity) <= 0.85, unless the change lowers load |
| Churn guardrail | Extra churn probability per exposed customer over 28 days <= 0.5 pp at the one-sided 95 % bound; raises only |
| Segment churn guardrail | The same per segment: starter 0.8 pp, growth 0.5 pp, enterprise 0.3 pp (segments with >= 20 exposed customers) |
| Churn-risk ceiling | No raise may take a segment's projected voluntary 28-day churn above 10 % |
| Change frequency | No exposed customer may see more than 3 price-change dates in 90 days, this one included |
| Bill share | Freeze a product below 5 % of the experiment customers' bills: its churn response is unidentified |

Gates that freeze before optimising: no randomised evidence for the product, evidence older
than 365 days, a material tier (> 5 % of demand) with a non-negative elasticity or posterior
SD > 0.25, or a forecast 80 % band wider than the point forecast. After optimising, a change
also needs P(improvement) >= 0.90. Only a fresh forecast can move a price.

Every constraint is checked on the exact integer price, and Hypothesis property tests compare
the optimiser against an independent scalar reference implementation
(`tests/pricing/reference.py`).

## Customer value

The churn hazard and CLV per segment come from `praxis.customer`, trained point in time on the
marts. Definitions, the champion rule and what the tests prove are in
[customer-value.md](customer-value.md).

## Audit, modes and execution

- The record holds input model versions, the forecast feature date, a market snapshot hash,
  every candidate with its objective terms and violated constraints, the chosen price,
  rejected alternatives with reasons, guardrail outcomes and the policy version. Its id is a
  content hash, so a retried cycle is a no-op and a conflicting second decision is refused.
- **shadow** (default) records only. **recommend** is executable after a named approval.
  **execute** additionally needs `allow_execute = true` in the policy.
- A Postgres trigger refuses any execution without a matching `change` record, at another
  price, in shadow mode, or unapproved in recommend mode, and execution re-verifies the
  record's checksum. A price freeze blocks execution the same way.
- The pricing cycle has no LLM dependency.

## Commands

```bash
make pricing-dev                                  # seed-1 forecast, elasticity, churn, customer model, shadow
make pricing-data customer-train pricing-shadow PX_SEED=1
python -m praxis.pricing decide --db WH --forecast-model DIR --elasticity-model DIR \
    --elasticity-report REPORT --churn-response CHURN.json --customer-model DIR --as-of DATE
python -m praxis.pricing {approve|execute|show} --database-url URL --decision-id ID
```

## Shadow evaluation

The shadow world continues the forecast world for eight weekly cycles. The simulator's own
equations, through a read-only observer, score every candidate price of every decision on its
seven days with the world's actual customers and service state. Acceptance gates safety
(constraints, no execution, complete records, lineage, the churn guardrail in truth), no harm,
direction of changes and calibration (`configs/pricing/shadow_acceptance.toml`).

## Known failure modes (measured)

- **Churn evidence is weak.** A 28-day test measures the churn response to about the size of
  the effect itself, so raises once cleared the confidence rule mainly in worlds whose test
  read low. The robust class above is the mitigation.
- **Churn transport.** Churn measured early in a year understated later churn by 1.5 to 2.3x,
  because demand growth raised utilisation and degraded service raises the base hazard. The
  model now scales its evidence by the current churn rate.
- Contribution predictions are accurate (predicted / true 1.04 held-out).
- Cross-price effects are assumed absent, which is true in this simulator.
