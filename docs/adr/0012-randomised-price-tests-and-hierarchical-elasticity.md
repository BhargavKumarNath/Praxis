# ADR 0012: Elasticity from randomised price tests, log-log slopes and a hierarchical Bayesian model

Status: Accepted (Phase 5). Written 2026-10-06, before the held-out evaluation world (seed 42)
was simulated. The "Amendment" section records the one change made after development runs,
also before seed 42.

## Context
Phase 6 needs the causal response of demand to price for each customer segment, with
uncertainty. A regression of demand on observed list-price changes would not provide it:
list prices change at chosen times (a launch, a cost change), so the coefficient mixes the price
effect with trends, seasonality and whatever made the business change the price
(`project.md` s6.2). The simulator knows each customer's latent own-price elasticity
(`population.elasticity`, a log-log slope), so recovery can be measured.

## Decision

### Identification: randomised price tests
* The business runs **randomised price tests**: the unit is the customer, each test randomises
  50/50 with its own salt, and treated customers are charged `ratio x` the list price for four
  weeks. Arms come from `praxis.domain.experiments.assign_arm` (BLAKE2b of `salt:unit`). The
  simulator and the analysis use the same function, so a logged arm can be audited against a
  recomputed one.
* The business-side record of each test (`configs/experiments/*.toml`: unit, salt, split,
  treatment / control price, assignment date, outcome window) is separate from the world
  that executes it (`configs/simulator/scenarios/elasticity_eval.toml`). A test checks they
  agree.
* Evaluation world: 8,000 customers, a 28-day untreated pre-period, then five concurrent tests
  (one per product, +20% or -1/6, so |log ratio| = 0.1823 for every test) for 28 days.
  Independent salts make it a 2^5 factorial, which allows a direct interference test.

### Estimand and estimators (`praxis.elasticity`)
* Unit = (test, customer). **Eligible** only on pre-treatment information: created before the
  pre-period, not churned before assignment, >= 56 requested units in the pre-period (2/day,
  away from the small-count regime where log counts are biased). Late arrivals are never
  eligible: price changes conversion, so they are selected by treatment (measured: the
  late-arrival SRM is reported).
* Outcome `delta` = log((Y_post + 0.5) / active days) - log((Y_pre + 0.5) / 28), Y = requested
  units (served + throttled). Churn ends the active window; < 14 active days = missing outcome.
* **Log-log baseline**: OLS of `delta` on the assigned log price ratio with test fixed effects
  (one slope per segment, fixed effects nested in segments), CR1 cluster-robust SEs by customer,
  t(G-1) intervals. The slope's estimand is the design-variance-weighted mean elasticity of the
  analysed units, w_u = pi (1 - pi) (log ratio)^2. Ground truth is computed with the same
  weights over the same units.
* **Segment interactions**: slopes by tier (at signup), industry and tier x industry cell
  (cells need >= 50 units).
* **Partial pooling**: empirical Bayes (Paule-Mandel) as a closed-form cross-check, and a
  **two-stage hierarchical Bayesian model in PyMC**: unpooled cell slopes b_c (with their
  cluster-robust SEs) ~ Normal(theta_c, se_c), theta_c ~ Normal(mu + a_tier + b_industry,
  sigma_cell). Tier and segment summaries are design-weighted averages of the cell draws.
  Why Bayesian: small cells borrow strength from their tier and industry, and the uncertainty
  of how much to pool (sigma_cell) is propagated rather than plugged in.
* **Contamination**: the assigned ratio instruments the price actually charged (2SLS / Wald,
  cluster-robust). Intention-to-treat stays the primary estimate; IV is reported per test.
* **Naive pre/post** (treated units only, no control) is reported as a failure case, never used.

### Gates
* Truth-free gates in the analysis (`configs/elasticity/elasticity.toml`): SRM (chi-square,
  p >= 0.001), assignment audit (0 mismatches), exposure logging (0 missing / conflicting),
  contamination (0) and price errors (0), omnibus pre-treatment balance (p >= 0.001), missing
  outcomes (<= 10% per arm, |difference| <= 2 pp); Bayesian diagnostics (R-hat <= 1.01, bulk and
  tail ESS >= 400, 0 divergences, posterior predictive p-values in [0.025, 0.975], prior
  sensitivity of pooled / tier elasticities <= 0.5 posterior SD). The CLI refuses to save an
  artifact unless all pass.
* Ground-truth acceptance in `praxis.science` (`configs/elasticity/acceptance.toml`): sign
  (pooled CI and tier 95% intervals below 0), pooled relative error <= 10% and |z| <= 3, tier
  relative error <= 25%, ordering of every identifiable segment pair (|truth difference| >= 3
  posterior SD) with posterior probability >= 0.95, >= 70% of cells' truth inside their 90%
  interval, hierarchical cell RMSE <= unpooled. A contamination world must show detected
  contamination within 3 pp of the configured rate, IV relative error <= 15%, and |ITT| < |IV|.
* All five files are pinned by hash in `tests/elasticity/test_config.py`.

### Architecture
* `praxis.elasticity` sits in the model layer beside `praxis.forecasting` and is added to
  "Models never import the simulator". `praxis.science` is a new **top** layer (sibling of
  `praxis.api`), the only package that joins model output with ground truth; model packages are
  forbidden from importing it.
* The simulator's `Intervention` gains `contamination_fraction` (failure injection). It is
  excluded from the canonical JSON when 0, so every existing world keeps its `config_hash`,
  event ids and golden checksum (verified by the existing pins).
* New dependency: PyMC 6.3 (+ PyTensor, ArviZ 1.3, numba). Listed in the recommended stack
  (`project.md` s8.1). SciPy (already locked) is now a direct dependency.

## Amendment (2026-10-06, development and synthetic worlds only, before seed 42)
As first registered, the hierarchical model learned the tier and industry scales
(`a_tier ~ ZeroSumNormal(sigma_tier)`, `sigma_tier ~ HalfNormal(tier_sd)`, same for industry), sampled with
`target_accept = 0.95`. The divergence gate (max 0) failed on the development world (seed 1):
2 divergences, located at sigma_industry ~ 0.02 (a centred-funnel). Parameterisations tried:

| Variant | Dev world (seed 1) | Synthetic additive world |
| --- | --- | --- |
| learned scales, centred, non-centred cells (registered) | 2 divergences | - |
| learned scales, tier + industry non-centred | 6-26 / 4 seeds | - |
| learned scales, centred, cells marginalised | 0-5 / 8 seeds | - |
| learned scales, tier centred, industry non-centred, cells marginalised | 0 / 8 seeds | 15-64 / 4 seeds |
| **fixed main-effect scales, cells marginalised, target_accept 0.95** | 0 / 6 seeds | 1-10 / 6 seeds |
| **same, target_accept 0.99** | 0 (final run) | 0 / 4 seeds; full analysis 0 / 3 seeds |

A between-group SD learned from three tiers or six industries is barely identified, and its
funnel moves between centred and non-centred forms depending on how precisely the effects are
measured. The amended model keeps the partial pooling where it matters, the tier x industry
interactions (sigma_cell, learned), and gives the main effects fixed weakly informative priors:
`a_tier ~ ZeroSumNormal(tier_sd = 1)`, `b_industry ~ ZeroSumNormal(industry_sd = 1)`.
theta_c is integrated out (b_c ~ Normal(m_c, sqrt(se_c^2 + sigma_cell^2))) and drawn afterwards
from its exact conjugate conditional. `target_accept` is 0.99. **No diagnostic gate, validity
threshold or acceptance threshold changed.** `configs/elasticity/elasticity.toml` was amended
and re-pinned. Prior sensitivity still refits under "wide" and "narrow" main-effect and cell
priors.

## Consequences
* Phase 6 consumes `data/models/elasticity/<version>/estimates.json` (pooled, tier, industry, cell
  posteriors, plus the tested price-ratio range). Elasticity is identified only within the tested
  range (|log ratio| = 0.18); extrapolating beyond it is the optimiser's risk to bound.
* The hierarchical model is two-stage: it treats the cell SEs as known. Cells are large
  (>= 50 units; ~900 on average at 8,000 customers), which keeps that approximation good.
* Simultaneous tests on one customer interact only through churn and shared capacity in this
  world. The factorial interaction check measures it rather than assuming it away.
* PyMC adds ~14 transitive packages (numba / llvmlite included) and C compilation on first use.
