# ADR 0016: Customer value, churn hazard and long-horizon pricing guardrails

Status: Accepted (Phase 9). Written 2026-10-10, after development on seed 1 only and BEFORE
any seed-44 world (experiment, forecast or pricing) was built. Pre-registered and hash-pinned:
`configs/customer/value.toml` (`tests/customer/test_config.py`), the amended
`configs/pricing/policy.toml` and the shadow acceptance comment registering seed 44
(`tests/pricing/test_config.py`). Also amends ADR 0013 (third amendment, below).

## Context
Phase 6 left one owner-assigned defect: the optimiser was safe but never moved a price
("PHASE 9 MUST FIX", `project_progress.md`). The churn input was the weak link:

* the churn response was a per-product relative slope from each product's own randomised
  test, measured to +-0.4-0.5 around ~0.3 (z < 1);
* every extra churner was valued at one AVERAGE customer lifetime value (a proxy:
  daily contribution x min(1 / hazard, 365 days), ~GBP 13k), although price-driven churners
  are disproportionately low-value starter customers;
* the hazard level came from the churn rate observed in the marts (incl. involuntary churn).

Phase 9 must also deliver what `project_plan` asks of customer value: a churn / hazard model,
retention metrics, a CLV baseline, segment-level value, pricing exposure history; and the
optimiser must be able to include a churn-risk ceiling, a CLV penalty, customer-level
price-change frequency and segment guardrails.

## Decision

### Customer value model (`praxis.customer`, model layer)
* **Label** (`configs/customer/value.toml`): VOLUNTARY churn within 28 days after a snapshot
  date T, for customers subscribed before T and not churned before T. Involuntary (payment)
  churn ends exposure without an event (competing risk) and is modelled separately per tier.
  Snapshots every 28 days (non-overlapping windows, piecewise-exponential likelihood), aligned
  to end at the training cutoff.
* **Point in time.** Every feature reads `marts.*` strictly before T. The tier and fee in force
  come from the new `fct_subscriptions` mart (starts + changes); `dim_customer.current_tier`
  (the tier at the END of the data) is never read. Labels live in a separate function.
* **Features** (`customer_features.v1`): tier, log tenure, log daily spend, usage trend (last 14
  vs previous 14 days), failed payment attempts (56 days), the customer's region error rate and
  p95 latency (7 days). Price-change history is deliberately NOT a feature: list changes hit
  everyone at once, so it is collinear with time; the causal price response comes from the
  randomised tests.
* **Models.** A ridge Poisson GLM (piecewise-exponential proportional hazards) and the baseline
  tier rate table (pooled toward the overall rate). **Champion rule, pre-registered:** the GLM is
  champion only if its summed Poisson deviance on a rolling-origin backtest (last 3 windows,
  each fitted on the windows that end before it) is below the table's; otherwise the table.
  The shared Newton solver lives in a new foundation module `praxis.glm` (used by the hazard
  model and by the churn response; they are independent siblings).
* **Recent calibration** at decision time: the champion applied to the snapshot one window
  earlier vs the voluntary churn observed since (all before T):
  factor = (O + 0.5) / (E + 0.5), SD(log factor) = 1 / sqrt(O + 0.5). Price multiplies the
  hazard (ADR 0013 second amendment), so the LEVEL must be current.
* **CLV** per customer, closed form: m * sum_{t=1..365} d^t exp(-(h_vol + h_inv) t), m = daily
  usage contribution + accrued tier fee, x (1 - payment loss); d = 1.10^(-1/365). The 365-day
  horizon equals the Phase 6 proxy cap, so the switch changes the method, not the horizon;
  sensitivity to discount rate {0, 5, 10, 20}% and horizon {90 .. 1095} days is reported.
* **Monitoring.** Segment report (tier): churn over the last window vs expectation, 90-day
  retention, CLV and its sensitivity, price-change exposure, alerts (window churn > 10%,
  observed / expected outside [1/1.5, 1.5]) as structured logs. CLI `python -m praxis.customer`.
* Artifact (`churn-{glm|table}-<hash>`): model version, data / feature / config versions, code
  revision, parameters, backtest metrics, file checksums, creation time.

### Causal churn response to the bill (`praxis.elasticity.churn`)
Per test p, customer i in its treatment arm has log price change a_ip; s_ip is the product's
share of the customer's PRE-PERIOD usage value. Then s_ip a_ip is the first-order log change of
the bill caused by test p, and

    h_i = h0_tier exp(c z_i + sum_p gamma_p s_ip a_ip)       (voluntary churn in the test window)

is estimated jointly (Poisson ML, tier baselines, standardised log pre-period spend as a
precision covariate; SE = max(model, sandwich)). The five concurrent tests form a 2^5
factorial, so gamma_p are identified by randomisation. On the development experiment world this
gives a pooled response 2.82 +- 0.86 (z 3.3) where the Phase 6 per-test relative slopes were
noise-dominated; heterogeneity across products Q = 1.5 (p 0.82). The document
(`churn_response.json`) is checksummed and tied to the gated Phase 5 report.
**Known misspecification:** the simulator's churn weights each product by a LATENT mix share;
the observable bill share overweights premium and underweights gpu (gpu's response is
understated by ~2x on development truth). Reported, not corrected: correcting it would tune to
the simulator.

### Optimiser integration (ADR 0013, third amendment)
* Evidence: gamma_p partially pooled across products (the existing empirical Bayes,
  `pool_slopes`). Response SD = hypot(posterior SD, gamma x SD(log calibration factor)).
* Per decision and segment g (tier in force), over the product's exposed customers:
  `churn_exposure_g = sum_i q_i s_ip` (q_i calibrated baseline 28-day voluntary churn
  probability), `window_churn_g = sum_i q_i`, and `CLV_g` = the CLV of the customers the price
  would lose (weighted by q_i s_ip; the segment mean if none). Extra voluntary churners of g at
  log price change x: `response x churn_exposure_g x x`.
* **CLV penalty** (objective): `response x sum_g CLV_g churn_exposure_g x / 28` per day,
  integrated over the response's posterior grid as before.
* **New hard constraints** (`[long_term]` in the policy; raises only, at the response's
  one-sided 95% bound): `segment_churn_guardrail` (extra churn per exposed customer of the
  segment <= starter 0.8 pp / growth 0.5 pp / enterprise 0.3 pp; segments with < 20 customers
  are reported, not judged), `churn_risk_ceiling` (no raise may take a segment's projected
  voluntary 28-day churn above 10%), `price_change_frequency` (no exposed customer may see more
  than 3 dates with a price change in 90 days, this one included; history from the new
  `fct_customer_price_changes` mart). The global `churn_guardrail` (0.5 pp) stays.
* No existing threshold changed (pinned by `test_phase9_did_not_loosen_any_phase6_threshold`).
  The CLV proxy cap (`valuation.clv_lifetime_cap_days`) is removed with the proxy.
* Records (format 2) carry the segment inputs, the per-segment projections and the
  change-frequency histogram; lineage adds `customer_model_version`, `customer_snapshot`,
  `churn_response_version`. Missing customer values make every decision UNAVAILABLE
  (`model_unavailable` / `input_unavailable`); a new stress case checks it.

### Shadow evaluation
The decision now values churners by segment, so truth does the same: true extra churners are
counted per tier (the simulator's current tier each day) and valued at the CLV the decision
recorded for that tier. Valuation = the decision's; responses = the simulator's (unchanged
principle). Reported, not gated: predicted vs true extra churners of the +max-step candidates,
binding constraints, and the calibrated hazards vs the TRUE latent hazards per tier.
**Acceptance thresholds unchanged.**

### Held-out protocol (pre-registered here)
Seeds 42 and 43 are spent for pricing. **Seed 44** is the Phase 9 held-out evaluation, built
once through every gate in order: elasticity experiment world (Phase 5 validity + Bayesian
gates must pass or no evidence exists) and its churn response; forecast world (Phase 4
backtest acceptance must pass or no artifact exists); pricing shadow world; the customer model
trained on that world's 196-day prefix; one shadow evaluation (`shadow_acceptance.toml`).
Change share and captured share are reported next to the gated checks.

**Controlled example** (gate item "long-term guardrails demonstrably alter unsafe short-term
decisions"): the Phase 6 seed-42 decisions (17 changes, 13 lost value) against the Phase 9
method on the same seed-42 worlds, run AFTER seed 44 and never used to tune
(`make pricing-compare`). Plus unit cases (`tests/pricing/test_long_term.py`).

## Development results (seed 1; the only world used to build the method)
All gated checks pass; 39 hold, 1 frozen, 0 changes. The new guardrails bind (frequency in the
first cycle, segment / ceiling as churn rises). Predicted vs true extra churners 1.22 (errs
safe). Calibrated hazards vs truth: tier ratios mostly 0.9-1.1, log correlation 0.68-0.87. The
dev response draw (2.8) is high relative to the effective truth on this world, so raises look
unprofitable (P(improve) <= 0.30). Missed value in truth: GBP 683 per day (premium and api
raises; late-window cuts are blocked by the capacity constraint, correctly).

**Power analysis (dev only, scratch world):** an 84-day factorial instead of 28 days gives 622
instead of 221 voluntary churn events and SE 0.51 instead of 0.86, but the best dev raise would
still sit near P(improve) 0.7: true raises are only modestly profitable once valuable churners
are priced in. A powered churn experiment is therefore NOT added in Phase 9 (cost: 12 weeks of
+20% prices for half the experiment population); it remains the owner's option.

## Consequences
* Prices will move only when a move is confidently profitable at segment CLV; on development
  that never happens. This is an honest outcome, not a defect to tune away.
* The customer model is trained on the decision world's own history (1,000 customers, ~140
  voluntary churners in the prefix), so the GLM rarely beats the tier table; the calibration
  factor carries the level.
* Ceiling, segment limits and frequency are business rules; their values were set from their
  rationale before any Phase 9 decision was computed, never from a result.

## Outcome (2026-10-10)
* **Seed 44 (held-out, run once): FAIL** on `no_harm` (-GBP 81 per day) and `direction` (0 / 5).
  Every safety check passed. The 5 changes were gpu_minutes +4.9% raises; contribution was
  predicted exactly, the churn cost ~2.7x too low: the bill-share misspecification named above,
  before the run. Seed 44 is spent for pricing. No re-run, no tuning.
* **Controlled example (seed 42): PASS.** The Phase 6 method's 17 changes (13 losing, -GBP 3,526
  per day) all became holds / freezes; the churn-risk ceiling and segment guardrails blocked the
  raises although the seed-42 churn evidence was again a low draw (predicted / true 0.50).
* Phase 9 stops here for the owner's decision (options in `docs/evidence/phase-9.md`).

## Amendment (2026-10-10, after the seed-44 FAIL; development world only; owner-approved option)
**Diagnosis** (seed-44 truth, after the single run): every change was a gpu raise whose churn
cost was understated ~2.7x. gpu is ~2% of the experiment customers' bills (all worlds:
0.021-0.022; every other product >= 0.147), so its +-18% test moves a customer's bill by
< 0.5%: the test carries almost no information about gpu's own churn response, and the pooled
bill response applied to a 2% share is an assumption, not a measurement.

**Method change:** `evidence.min_bill_share = 0.05` in the policy. A product whose mean bill
share in the experiment that measured its response (`churn_response.json`, `mean_share`) is
below 5% has an UNIDENTIFIED churn response: the evidence gate freezes it
(`churn_response_unidentified`; the reason is accumulated with any other freeze reasons). The
threshold follows from the design (5% x 0.18 < 1% bill change), not from a result. Considered
and rejected: "the larger of the product's own and the pooled response" (conservative for raises
but it credits cuts with an inflated churn reduction). Nothing else changed; no threshold was
loosened.

Development world (seed 1) with the amendment: all gated checks pass; 32 hold, 8 frozen (gpu).

**Fresh held-out world, pre-registered before it was built:** seeds 42, 43 and 44 are spent.
The re-evaluation uses **seed 45** for every input world, built once each through its own gate
(experiment world: Phase 5 gates; forecast world: Phase 4 acceptance; pricing world; customer
model on the prefix), then one shadow evaluation with `shadow_acceptance.toml` unchanged.

## Outcome of the amendment (seed 45, run once): FAIL (direction)
`no_harm` PASS (+GBP 13.9 per day), `direction` FAIL (4 / 7 = 0.57 < 0.80); every safety check,
calibration (1.05) and stress case passes; gpu frozen as designed (8 / 8). The 3 losing changes
were cpu +4.5% raises (true -GBP 31 to -34 per day each; the 4 data-transfer raises gained). Cause
(truth, after the run): the seed-45 pooled bill response is a low draw (1.26 +- 0.86 vs 2.8 dev,
1.83 seed 44); cpu's own test read 5.2 +- 3.1 but heterogeneity was not detectable, so pooling
pulled cpu to the low common value; predicted / true extra churners 0.79. The confidence rule
cannot absorb a response measured to +-70% of its size. Seed 45 is spent; nothing re-run.

## Second amendment (2026-10-11, after the seed-45 FAIL; development worlds only)
Owner instruction: fix Phase 9 properly. Investigation on 22 FRESH development worlds (seeds
101-124; 106 and 108 failed their Phase 4 forecast gate) with latent truth, after every
failure diagnosed before any change. Root cause: the decision's P(improvement) was
overconfident (P >= 0.90 claimed, ~75-80% realised). Six causes, each fixed with in-world data:

1. **Plug-in pooling** (EB, tau-hat = 0 with five noisy tests) hid real product differences
   (seed 45: cpu). -> Full-Bayes normal hierarchical model, tau ~ half-Cauchy(1) integrated on a
   grid (`hierarchical_pool`; Gelman 2006, Polson and Scott 2012). Policy `churn_tau_scale`.
2. **Decisions flipped with the uncheckable degree of pooling** (dev 101: data's own test
   -2.4 +- 1.9 vs truth +2.4). -> Robust (maxmin) decision over the credible class
   {hierarchical, complete pooling, no pooling}: a candidate is scored under its least
   favourable posterior; P(improvement) is the minimum; guardrails use the largest upper
   bound (Gilboa-Schmeidler multiple priors). Policy `churn_models`. Record format 3.
3. **Transport of exposure**: today's spend share understates products whose price rose since
   the test (customers buy less). -> Laspeyres reference shares: the tested basket valued at
   today's prices (`reference_shares`, deflated with the causal tier elasticity).
4. **Secant regressor**: s log r biased cut-tested products ~8% low and raise-tested ~10% high
   at +-18%. -> exact log bill change log(1 + s (r - 1)) (`churn.py`, RESPONSE_FORMAT 3).
   Verified against latent truth: experiment targets now match the decision world (api 1.57 vs
   1.57, premium 1.26 vs 1.20; cpu / data gaps 15% / 10%).
5. **Book-level elasticity uncertainty omitted** (dev 111 api: book -0.95 vs population -0.86,
   contribution -17%). -> Var(e_i) per tier from the randomised arms (Var(T) - Var(C) = x^2
   Var(e); recovers truth: starter 0.553 vs 0.526), book SD eta = sqrt(Var x sum w^2) added to
   the tier posterior (`heterogeneity.py`, `book_elasticity`).
6. **Hazard calibration lag** (level centred 14 days back, hazards rising 5-8% a week; churn
   under-predicted ~12%). -> Poisson trend over 8 weekly windows, N(0, 0.1^2) slope prior,
   forecast at the middle of the decision week (`value.toml [calibration]`).

Rejected with evidence: an 84-day churn experiment (frailty selection attenuates the hazard
ratio: response z mean +0.35, coverage 0.82), an in-world difference-in-differences elasticity
(confounded by spikes), the forecast band as contribution SD (overstates). No acceptance or
policy threshold changed; constraints were only added.

**Development operating characteristics (22 worlds, method frozen afterwards):** 21 / 22 pass
the shadow acceptance; 22 changes, 19 improved in truth, +GBP 1,076 per day; response
posterior z sd 0.6-0.7, 90% coverage 1.00 (conservative). The failure (102): one late week of
an otherwise right data-transfer call fell below break-even (3 / 4).

**Held-out protocol (pre-registered before any was built):** seeds 201, 202, 203, each built
through every gate and evaluated once; Phase 9 passes only if all three pass the unchanged
`shadow_acceptance.toml`. A world failing its Phase 4 forecast gate is replaced by 204, 205...

## Outcome of the second amendment (held-out 201-203, run once): FAIL
201 PASS (0 changes, 8 gpu frozen), 203 PASS (0 changes), **202 FAIL** (`no_harm` -GBP 49 / day,
`direction` 3 / 9). Every safety check passed in all three. Diagnosis (truth, after the run; nothing
re-run): the seed-202 experiment is an extreme low draw for churn (raw bill responses api -0.68,
data -0.78, all five near zero; pooled ~0.1 +- 1.0 vs truth ~1.5). All members of the robust class
use the same data, so all agreed churn barely responds and api / data raises cleared P >= 0.90;
contribution was predicted within ~15%. The method still treats "no evidence of a churn effect"
as "evidence of no churn effect". Not patched: any rule added now would be fitted to this result.
Recommended next step (owner): an informative, pre-registered prior on the bill response from
outside the single experiment (accumulated earlier tests or a monotone, positive economic prior),
then a fresh held-out set. Seeds 42-45 and 201-203 are spent for pricing.

## Third amendment (2026-10-11, after the seed-202 FAIL; development worlds only)
Owner instruction: investigate the 202 failure, fix it in a general way, finish Phase 9.

**Root cause (development evidence, latent truth used only to diagnose).** Nothing is wrong
with world 202's simulator: its true bill response (~1.5-1.7) is the same as every world's. A
28-day test holds ~230 voluntary churners and a +-18% price moves a customer's bill only ~3-4%,
so the pooled response is measured to +-0.85-0.95 and a single product's to +-1.3-3. With a
FLAT prior, the decision trusts whatever one test reads. The method's uncertainty is honest
across worlds (response z sd 0.66, coverage 1.0), but conditional on ACTING it is not: raises
clear P >= 0.90 mostly in worlds whose test read low (optimizer's curse: Smith and Winkler
2006). On the development sweep the churn cost of the changes that were made was under-
predicted ~2x; seed 202 (pooled 0.05 +- 0.84, an outlier against the 25 development
experiments' 1.7) is the extreme case. Predictions are unbiased on average; the bias comes from
selection on a single noisy test.

**Decision: a robust meta-analytic-predictive (MAP) prior, as one more member of the robust
class** (`praxis.pricing.churn_prior`; Neuenschwander et al. 2010; Schmidli et al. 2014,
Biometrics 70). A business that runs churn experiments keeps their results; the prior for the
next test is the predictive distribution of the earlier ones:

* **Archive** (`configs/pricing/churn_archive.json`, `make churn-archive`): the observable
  estimates (gamma, SE per product) of the 25 DEVELOPMENT experiment worlds (seeds 1,
  101-124), each verified by its own checksum. Never simulator truth; no evaluation seed
  (a test pins the archive's sources and its hash; the policy pins its checksum). The decided
  experiment is refused if it is in the archive.
* **Predictive prior per product**: normal random-effects meta-analysis of the archived
  estimates, flat mean, between-experiment tau ~ half-Cauchy(`churn_tau_scale`) on the same
  grid as the product pooling (now one shared helper); prior = N(E[mu], Var(mu) + E[tau^2]),
  the distribution of a NEW experiment's true response (api 1.65 +- 0.57, cpu 1.90 +- 0.99,
  data 1.37 +- 1.16, premium 1.68 +- 0.86).
* **Robust mixture**: (1 - w) MAP + w N(MAP mean, 4^2), w = 0.2 (`churn_prior_vague_weight`,
  `churn_prior_vague_sd`; a pre-specified weakly informative component; bill elasticities of
  churn beyond +-8 are implausible). A test that conflicts with the archive moves the posterior
  toward its own data and widens it, so the decision freezes rather than trusting either side.
* **`meta_analytic` joins the robust class** {hierarchical, complete pooling, no pooling}: a
  move must now be confident under the archive-informed posterior AS WELL. This is strictly
  more conservative than the second amendment; it never adds a move.
* Records: format 4 (the extra member in `alternatives`; lineage `churn_archive_version`).

**Development evidence (22 worlds, leave-one-out: each world decided with an archive WITHOUT
its own experiment):**

| Variant | Pass | Changes (right) | True GBP / day |
| --- | --- | --- | --- |
| second amendment (frozen method) | 21 / 22 | 22 (19) | +1,076 |
| sign restriction only (gamma >= 0, truncated posterior) | 20 / 22 | 9 (7) | +275 |
| MAP posterior alone (replaces the class) | 20 / 22 | 27 (22) | +1,237 |
| **MAP added to the robust class (adopted)** | **21 / 22** | **15 (13)** | **+804** |

Considered and rejected with this evidence: a sign restriction (removes right and wrong moves
alike; redundant once the archive is used) and the MAP posterior alone (more value, but two
worlds lose money: the optimizer's curse on a narrower posterior). The remaining development
failure (102) is the second amendment's: one late week of a right data-transfer raise fell
below break-even (3 / 4). By construction the adopted rule blocks the 202 failure mode (a test
reading "no effect" against an archive reading ~1.7 is no longer enough for a raise); this is a
property of the design, not a re-run of seed 202, which stays spent. No acceptance or policy
threshold changed.

**Held-out protocol (pre-registered before any was built):** seeds **301, 302, 303**, each
built through every gate (`make pricing-world SEED=n`: Phase 5 experiment gates, churn
response, Phase 4 forecast acceptance, pricing world, customer model on the prefix) and
evaluated once with the unchanged `shadow_acceptance.toml`; Phase 9's held-out gate passes only
if all three pass. A world failing its Phase 4 forecast gate is replaced by 304, 305, ...
Seeds 42-45 and 201-203 stay spent.

## Outcome of the third amendment (held-out 301-303, run once each): PASS
Each world was built through every gate (Phase 5 validity PASS, Phase 4 forecast acceptance
11 / 11 PASS, customer model on the prefix) and evaluated once with the unchanged
`shadow_acceptance.toml` (policy `policy-3b6775c87cd1`, archive `archive-d536efbb285ec3fd`):

| World | Result | Decisions | Best feasible, true GBP / day (missed) |
| --- | --- | --- | --- |
| 301 | PASS | 32 hold, 8 frozen, 0 changes | 162 |
| 302 | PASS | 23 hold, 17 frozen, 0 changes | 291 |
| 303 | PASS | 21 hold, 19 frozen, 0 changes | 1,038 |

Every gated check passed in all three (constraint compliance, no execution, complete records,
lineage, churn guardrail in truth, six stress cases, no harm, direction, calibration). Stated
plainly: **all three passes are holds and freezes; no held-out price moved.** The method's
ability to make value-adding moves is shown on development (leave-one-out: 15 changes, 13
right, +GBP 804 / day over 22 worlds), not on these held-out worlds, where it captured nothing
of GBP 162-1,038 / day available. Change share and captured share are REPORTED, not gated
(ADR 0013 amendment, owner-approved), so the held-out gate passes. Seeds 301-303 are now spent.
