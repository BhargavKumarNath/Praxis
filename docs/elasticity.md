# Price elasticity

Pricing needs to know how demand responds to price, and that is a causal question. Regressing
demand on the prices a business happened to charge mixes the price effect with whatever
prompted each change. Praxis estimates elasticity from randomised price tests instead, and
checks the estimate against the simulator's hidden truth. Decision: ADR 0012. Results:
[results.md](results.md#elasticity).

## Design

Customers are assigned to an arm by hashing `salt:customer_id`
(`praxis.domain.experiments.assign_arm`), and the treated arm pays a fixed ratio of the list
price. The evaluation world has 8,000 customers, a four-week pre-period, then five concurrent
50/50 tests, one per product, with independent salts (a 2^5 factorial). Three products are
raised 20 % and two are cut by the same log amount.

A unit is one (test, customer) pair. Eligibility uses pre-treatment information only: the
customer existed before the pre-period, had not churned, and used enough of the product.
Customers who arrive after assignment are never eligible, because price changes who converts.
The outcome is the change in log requested units per active day between the pre-period and the
test window.

Arms are recomputed from the salt and compared with the logged exposures, so each unit is
`ok`, `missing`, `arm_mismatch`, `contaminated`, `price_error` or `conflicting`.

## Estimand

The fixed-effect OLS slope over a set of units S estimates

```text
sum_u w_u e_u / sum_u w_u,   w_u = pi (1 - pi) (log ratio_u)^2
```

where e_u is the customer's own-price elasticity of requested units and pi the treated share.
Every test here has the same |log ratio| and pi, so this is the plain mean over the analysed
units. `praxis.science` computes the truth with exactly these weights over exactly these units.

## Estimators

| Step | Model | Role |
| --- | --- | --- |
| 1 | Log-log OLS with test fixed effects, cluster-robust by customer | Pooled estimate |
| 2 | Same, with one slope per tier, industry or cell | Segment interactions |
| 3 | Two-stage hierarchical Bayesian model in PyMC over the 18 tier x industry cells | The artifact the optimiser uses |
| - | IV, with the assigned ratio instrumenting the charged price | When contamination is detected |
| - | Naive before / after on treated customers only | A documented failure case, never used |

The hierarchical model lets each cell borrow strength from its tier and industry by an amount
learned from the data. It samples with NUTS (4 chains x 2,000 draws, fixed seed) and is
bit-reproducible. Main-effect scales are fixed because, with three tiers and six industries,
learned scales produced divergences.

## Gates

No artifact is published unless every truth-free gate passes (`configs/elasticity/elasticity.toml`):
sample-ratio mismatch, assignment and exposure logging, no contamination or price errors,
covariate balance, missing outcomes per arm, and the Bayesian diagnostics (R-hat, ESS,
divergences, posterior predictive checks and prior sensitivity).

Separately, `praxis.science` scores the estimates against truth with pre-registered criteria
(`configs/elasticity/acceptance.toml`): sign, pooled and tier magnitude, the ordering of every
identifiable segment pair, interval coverage, and that pooling does not make cells worse. A
contamination world additionally needs the contamination detected and the IV estimate close to
truth.

## Commands

```bash
make elasticity-data EL_SEED=1            # simulate, load, dbt (the default seed 42 is held out)
make elasticity-analyze EL_SEED=1         # validity gates, estimators, PyMC, artifact
make elasticity-evaluate EL_SEED=1        # ground-truth acceptance
make elasticity-contamination EL_SEED=1   # detection, ITT dilution and IV
make churn-response EL_SEED=1             # the churn response to price, from the same tests
```

## What the failure cases show

- **No control group.** The naive before/after absorbs the common demand trend: -0.97 against
  -1.24 on `api_requests` and -1.67 against -1.38 on `cpu_minutes`.
- **Contamination dilutes the ITT** by roughly the contaminated share. IV recovers the pooled
  truth within 0.5 %, but the gate still refuses the artifact.
- **Raise vs cut** estimates differ because they were tested on different products and
  customers, whose true elasticities differ. That is composition, not evidence of curvature.
- **Small counts** bias log outcomes differently by arm, which is why low-volume units are
  excluded before arms are examined.
- **Tested range only.** Elasticity is identified for |log ratio| <= 0.18, and the optimiser
  is not allowed to extrapolate past it.
