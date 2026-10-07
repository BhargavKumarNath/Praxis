# ADR 0013: Constrained pricing optimiser: causal response, hard constraints, audited shadow decisions

Status: Accepted (Phase 6). Written 2026-10-06, before the optimiser was run on the development
or the held-out shadow world. The policy (`configs/pricing/policy.toml`), the shadow world
(`configs/simulator/scenarios/pricing_shadow.toml`) and the shadow acceptance
(`configs/pricing/shadow_acceptance.toml`) are hash-pinned in `tests/pricing/test_config.py`.

## Context
Phases 4 and 5 produce a demand forecast and causal tier elasticities. Phase 6 must turn them
into a commercial decision that is safe, explainable and auditable (project_plan Phase 6,
`project.md` s6.3), and that can be scored honestly against the simulator. ADR 0004 already
fixes that the optimiser, not the LLM, owns the price.

## Decision

### Decision unit and objective
* One decision = the **list price of one product** for the next cycle (the business sets one
  list price per product; the simulator applies it to every customer). A cycle decides every
  product once. Weekly cycles, 7-day forecast horizon.
* Demand at a candidate price p: the forecast at the CURRENT price, per (region, tier),
  scaled by the tier's causal elasticity: `D_s(p) = B_s (p / p0)^e_t`. The forecast model's own
  price features are **not** used to price a change: they are predictive (they learnt from
  list-price changes the business chose), not causal (`project.md` s6.2).
* Objective per day (micro-GBP): contribution `sum_s D_s served_s (p (1 - L) - c_r)` with L the
  observed payment-loss rate and c_r the observed marginal cost (including the scarcity
  premium, so capacity cost is in the objective), minus a churn valuation
  `exposed x CLV x max(slope, 0) x log(p / p0) / window`. The churn slope is the randomised
  churn-rate difference per unit log price of the product's Phase 5 test; CLV is a labelled
  proxy (daily contribution per active customer x min(1 / churn hazard, 365 days)) until
  Phase 9 replaces it.
* Uncertainty: one standard-normal shock shared by all tiers (`e_t = mean_t + sd_t z`;
  comonotone, the conservative choice when only marginals are known) over a deterministic
  equal-probability grid. The optimiser maximises `E[delta] - 0.5 SD[delta]`.

### Hard constraints (checked pointwise on the exact integer price)
Price floor and ceiling (per product), max step x(1.05), 14-day cooldown (list-price changes
in the marts and executed decisions), margin floor 20% in every region served, capacity
(projected utilisation at the forecast's 90% quantile and the most demand-increasing
elasticity draw stays <= 0.85, unless the change lowers load), extrapolation limit
(|log(p / price when tested)| <= 0.1823, the tested range, ADR 0012), churn guardrail (extra
churn probability per exposed customer over the evidence window <= 0.5 pp at the one-sided 95%
bound; cuts are never credited).

### Gates before and after optimisation
* UNAVAILABLE: impossible inputs (validated), stale or missing forecast (only a FRESH
  forecast may move a price), model / cost / price unavailable, non-finite objective.
* FROZEN: no randomised evidence for the product, evidence older than 365 days, a material
  tier (> 5% of demand) with non-negative elasticity or posterior SD > 0.25, forecast
  80% band wider than the point, or P(objective improves) < 0.90 for the best move.
* INFEASIBLE: no candidate satisfies every constraint. HOLD: the current price is best (or
  cooldown). CHANGE: otherwise. A move away from an infeasible current price restores the
  constraints and is not confidence-gated.

### Search
Candidates: a 41-point log grid over the max-step range on the price tick, plus the current
price; then every tick between the best candidate's neighbours (sampled to 400) and a dense
polish of every tick around the best (exact boundary optima). Golden tests compare with
closed-form Lerner prices and with exhaustive enumeration; Hypothesis checks every returned
price against an independent scalar reference. No solver dependency (OR-Tools / scipy.optimize
were considered: the problem is one-dimensional per product, so exhaustive refinement is exact
at the tick and simpler to audit).

### Capacity coupling
Products share regional capacity. Each product may scale its regional demand by at most
`max_utilization / current_utilization` (when its change adds load), so the sum over products
can never exceed the limit: the per-product problems stay separable and jointly feasible.

### Audit and modes
* Every decision (all statuses) is a content-addressed record: input model versions, feature
  snapshot references (forecast feature date, market snapshot hash, price plan version),
  candidates with objective values, constraint limits, chosen price, rejected alternatives with
  reasons, reason codes, guardrail outcome, policy version (hash of the policy file + mode).
* Postgres migration 0002: `pricing_decisions` (append-only, one per cycle and product,
  canonical JSON text + SHA-256), `pricing_approvals`, `price_executions` (the price book).
  A trigger refuses any execution without a matching CHANGE record, at another price, in
  shadow mode, or in recommend mode without approval. Execution reads the record back and
  verifies its checksum.
* Modes: **shadow** (record only; the default and the policy's mode), **recommend** (execute
  after a named approval), **execute** (needs `allow_execute = true` in the policy).

### Shadow evaluation (`praxis.science.pricing_shadow`)
* World: the Phase 4 forecast world continued for 8 weekly cycles. The simulator gains
  `population.arrival_horizon_days` (hash-neutral when unset) so the 252-day world shares the
  exact population, hence the exact first 196 days, of the 196-day world the forecast
  artifact was trained on. The evaluation refuses to run unless an artifact's `data_version`
  equals the world's 196-day prefix panel.
* Truth: the simulator gains a read-only day observer; `Engine.expected_demand` and
  `Engine.churn_hazard` (which `run` itself uses) re-evaluate the day's equations at any price.
  Each decision is scored on its 7 days at every candidate it considered, with the same
  valuation inputs it used (L, CLV) and TRUE demand and churn responses.
* Evidence transport is part of what is measured: the elasticity comes from the 8,000-customer
  experiment world, the decisions price a different 1,000-customer draw of the same business.
* Pre-registered acceptance: 0 constraint violations (independent re-check from the record),
  0 executions, complete records, forecast lineage, >= 25% of decisions move the price,
  >= 80% of changes improve the true objective, >= 50% of the achievable true improvement
  captured, predicted / true contribution change in [0.5, 2], true extra churn within the
  guardrail for every change, and five stress cases (stale features, features too old,
  forecast model unavailable, evidence unavailable, inflated uncertainty) that must fail safe.
  Seed 1 for development, seed 42 run once.

### Architecture
`praxis.pricing` is a new layer between `api | science` and the model layer; it is added to
"Models never import the simulator" and to the web-framework contract. Coverage floor 95%
for the whole package (required_test.md s5: optimiser and guardrails).

## Amendment (2026-10-06, development world only, before seed 42, owner-approved)
The registered method used each product's churn slope from its own randomised test as a point
estimate (clipped at 0) and left its uncertainty out of P(improvement). On the development
shadow world (seed 1) every safety check passed, but **all 24 changes (+4.5% raises on cpu,
gpu, data transfer) lost value in truth** (sum of true objective deltas -GBP 5,652 per day;
predicted contribution was accurate, ratio 0.98). Cause: the single-test slopes are
noise-dominated (seed 1: +0.057, +0.006, -0.053, -0.012, +0.034 with SE 0.03-0.05), the true
response is ~0.02 per unit log price for every product, and the optimiser raised exactly the
products whose noise looked churn-free (winner's curse), valued at a CLV proxy of ~GBP 13k.

**Method amendment** (no threshold of the policy changed):
* product churn slopes are partially pooled across the tests (empirical Bayes: Paule-Mandel
  between-product variance, Morris posterior SD; `praxis.pricing.evidence.pool_slopes`);
* the objective uses the pooled posterior mean (no clipping) and the decision's SD, quantiles
  and P(improvement) integrate over the slope's posterior (an independent 21-point
  equal-probability grid next to the elasticity grid). The guardrail keeps the one-sided upper
  bound, raises only.

With the amended method the development world gave 0 changes (23 hold, 17 frozen at
P(improve) 0.69-0.71), no value lost, one real opportunity missed (premium_latency, true
+GBP 47 per day, predicted +GBP 42 per day but not confidently). The pre-registered shadow
acceptance presupposed movement (min change share 0.25, min captured share 0.50, direction and
calibration fail with no change), so it failed for the opposite reason. **Acceptance
amendment** (approved by the owner): gated = safety (unchanged), no harm (sum of true deltas
>= 0), direction >= 0.80 when there are changes, calibration in [0.5, 2] when there are >= 5
changes, churn guardrail in truth, stress cases; reported only = change share and captured
share. `shadow_acceptance.toml` re-pinned. The held-out seed 42 had not been simulated for
Phase 6 when this was decided.

## Outcome
Seed 43 (after the second amendment, run once): **PASS**, all 40 decisions hold, every gated
check passes, and the churn slope the optimiser used was within 1 SD of the truth on every cycle.
The optimiser is safe but does not yet find confident moves (`docs/evidence/phase-6.md`).

### First held-out run (seed 42, after the first amendment)
Gate **FAIL** (`docs/evidence/phase-6.md`): all safety checks passed and contribution was
predicted within 4%, but 13 of 17 changes lost value in truth. The seed-42 churn estimate was
low by ~1.3 SD, and the churn response is not transportable in absolute terms: price multiplies
the churn hazard, which is 1.5-2.3x higher in the shadow window than in the experiment window.
Seed 42 is spent for pricing; a fix must be evaluated on a new pre-registered held-out seed.
The policy stays in shadow mode.

## Second amendment (2026-10-07, after the seed-42 FAIL; development world only)
Diagnosis of the held-out failure (truth, after the single run): the churn response was
carried as an ABSOLUTE slope (extra churn probability per unit log price) measured in the
February experiment window, where the base churn hazard was ~0.00115 per day. Price multiplies
the hazard, and by the shadow window demand growth had pushed utilisation to ~0.95 and the base
hazard to 0.0017-0.0028, so the true absolute slope was 1.5-2.3x the transported one.

**Method change:** the evidence is a RELATIVE effect: per test, the log ratio of treatment and
control churn rates (+0.5 continuity correction) per unit log price, delta-method SE, pooled
across products by the same empirical Bayes estimator. At decision time the absolute slope,
its SD and its guardrail upper bound are the relative ones x the 28-day churn probability
observed in the marts just before the decision (`1 - exp(-28 x hazard)`). Both factors are
recorded on every decision. No policy value, gate or acceptance threshold changed.

Development world (seed 1) with this method: 40 holds, all gated checks pass; the scaled slope
follows the observed churn rate (0.048 -> 0.079 over the cycles).

**Fresh held-out world, pre-registered before it was built:** seed 42 is spent. The
re-evaluation uses **seed 43** for every input world, run once each: the elasticity experiment
world (8,000 customers, Phase 5 gates must pass or no evidence exists), the forecast world (Phase
4 backtest acceptance must pass or no artifact exists), and the pricing shadow world. The shadow
acceptance (`shadow_acceptance.toml`, thresholds unchanged) decides the Phase 6 gate. Seed 42
results stay in the evidence as the first, failed held-out evaluation.

## Consequences
* Prices move at most 5% per cycle and at most 18% from the tested price in total; a
  constant-elasticity world would reward larger moves, a real one might punish them.
* The churn response is the binding evidence gap: the Phase 5 tests measure it to +-0.016 per
  unit log price, about the size of the effect, so under a CLV valuation most moves cannot be
  shown to help and the optimiser holds. Phase 9 (hazard / CLV model, longer horizons) or a
  dedicated churn experiment is what would let prices move with confidence.
* Cross-price effects are assumed absent (true in this simulator: demand for a product depends
  only on its own price). Customer-level price discrimination is out of scope.
* No price executes in Phase 6 outside tests: the shadow world keeps its prices.
