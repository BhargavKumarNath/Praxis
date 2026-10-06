# Phase 5 Evidence Report

```text
Phase: 5 - Price Elasticity and Causal Price Science
Date: 2026-10-06
Code revision: 863009a + working tree (reports and the artifact record "863009a-dirty"); no commits
               made by Claude
Environment: Linux, Python 3.12, numpy 2.5.3, scipy 1.18.1, PyMC 6.3.2 (PyTensor 3.3.3, ArviZ 1.3),
             duckdb 1.5.6, dbt-duckdb (Phase 2), 16 cores (PyMC: 4 chains, cores=1)
All customers, prices, demand and results are SYNTHETIC (Phase 1 simulator). No cloud resource was
created or used.
```

Design: `docs/elasticity.md`, ADR 0012. Raw results: `phase-5-eval-seed42-report.json`,
`phase-5-eval-seed42-recovery.json`, `phase-5-contamination-seed42-recovery.json`,
`phase-5-dev-seed1-recovery.json`, `phase-5-artifact-manifest.json`.

## Protocol (in order)

1. **Pre-registered** before any experiment world was simulated: the evaluation world
   (`configs/simulator/scenarios/elasticity_eval.toml`: 8,000 customers, 28 pre-period days, five
   concurrent 50/50 tests at +20% / x 1/1.2 for 28 days), the contamination world, the experiment
   registry, the analysis rules and truth-free gates (`configs/elasticity/elasticity.toml`), and the
   ground-truth acceptance (`configs/elasticity/acceptance.toml`). All five files are hash-pinned.
2. **Development world (seed 1)** only, while writing the method:
   * validity gates passed first time; the Bayesian divergence gate **failed** (2 divergences at
     sigma_industry ~ 0.02). Six parameterisations were compared on the dev world and on a synthetic
     world with exactly additive cells (table in ADR 0012). Learned main-effect scales funnelled in one
     world or the other. **Amendment** (ADR 0012): fixed main-effect prior scales, cell effects
     marginalised and redrawn exactly, `target_accept` 0.95 -> 0.99. No gate, validity threshold or
     acceptance threshold changed. `elasticity.toml` re-pinned.
   * with the final method, all 8 recovery checks passed on dev (pooled -1.355 vs truth -1.337) and all
     4 contamination checks passed on the dev contamination world.
3. **Evaluation world (seed 42)**, run once with the final method: `make elasticity-data
   elasticity-analyze elasticity-evaluate`, then `make elasticity-contamination`. All checks passed.
4. **Post-run diagnostic fix** (a reported check, not a gate). Reviewing the seed-42 report, the
   factorial **interference** flag fired on the two price-cut tests (interaction p ~ 1e-7, 3e-5). The
   same flags were already in the dev report, unreviewed. Cause: the statistic summed the customer's
   raw assignments in the other tests, whose mean depends on which products the customer uses, and
   product mix correlates with elasticity. Centring each assignment on its design mean removes it
   (all p >= 0.21 on seed 42). Fixed with a regression test
   (`test_product_mix_heterogeneity_is_not_mistaken_for_interference`). The seed-42 analysis was then
   re-run on the same warehouse: only the 22 interference fields changed. `data_version`, every
   estimate, the artifact version and the recovery result are byte-identical (diffed).

## Tests executed

`make check` exit 0 (ruff, ruff format, mypy strict, import contracts, pytest with coverage against
local Postgres and the Pub/Sub emulator, per-module coverage floors, `events-check` chaos smoke, schema
sync, secret scan, pip-audit + npm audit, frontend, terraform validate, actionlint + zizmor):
**801 passed, 1 skipped** (live BigQuery module, `make bq-verify` only) in 543 s; total coverage 98.88%;
all 89 per-module floors met; 4 import contracts kept. pip-audit and `npm audit` clean.

`make nightly-science` exit 0: Phase 4 forecast acceptance (dev world) unchanged (hybrid vWAPE ratio 0.775);
Phase 5 dev world recovery and dev contamination world pass. Rebuilding the dev world from scratch
reproduced `data_version` `units-796f4147a09902f9` and artifact `elasticity-hier-b45b16d03f4f` exactly
(simulate -> dbt -> analysis -> PyMC is bit-reproducible). `make nightly-security` exit 0 (no known
vulnerabilities, gitleaks: no leaks in committed history, trivy: 0 Terraform misconfigurations).

A first `make check` run failed the coverage gate on a floor added in this phase
(`science/__main__.py` 93.3% < 95%, the `python -m` entry line); covered by
`test_module_entry_points` rather than lowering the floor.

Phase 5 adds 101 tests in `tests/elasticity/` (3 of them integration: simulator -> DuckDB -> dbt ->
CLIs). Coverage: `domain/experiments`, `elasticity/{analysis,artifact,bayes,config,dataset,estimators,
validity,warehouse}` 100%, `science/elasticity_recovery` 99%, both CLIs >= 93%. New floors at 95%:
`elasticity/{validity,dataset,estimators,artifact}.py`, `science/*`.

| Required (required_test.md s11) | Test |
| --- | --- |
| Ground truth: sign recovery | `sign` check (seed 42 below); `test_each_preregistered_check_can_fail[sign-*]` |
| Ground truth: aggregate magnitude | `pooled_magnitude` (<= 10% and abs(z) <= 3), `tier_magnitude`; `test_pooled_log_log_estimate_recovers_the_weighted_truth`, `test_pooled_magnitude_also_needs_statistical_agreement` |
| Ground truth: segment ordering where identifiable | `segment_ordering` (pairs with abs(truth diff) >= 3 posterior SD); `test_ordering_needs_at_least_one_identifiable_pair`, `test_tier_summaries_and_pairwise_ordering` |
| Ground truth: interval coverage | `cell_interval_coverage` (90% intervals); `test_cluster_robust_intervals_have_nominal_coverage` (Monte Carlo, 400 worlds, unit- and cluster-level assignment) |
| Deterministic assignment with fixed salt | `test_assignment_is_deterministic_for_a_fixed_salt`, `test_assignment_value_is_pinned`, `test_simulator_logs_the_arm_the_domain_function_assigns` |
| Mutually exclusive assignment | `test_every_unit_gets_exactly_one_arm` (Hypothesis, 200 cases), registry rejects overlapping tests on one product, `test_different_salts_assign_independently` |
| Exposure logging | `test_exposure_status_classification` (6 statuses), `test_exposure_logging_gap_is_detected`, `test_assignment_audit_catches_wrong_logged_arms` |
| Sample ratio mismatch detector | `test_srm_detects_lost_treated_units` (15% treated loss), `test_srm_statistic`; late-arrival SRM reported |
| Missing outcomes | `test_churn_shortens_the_window_and_short_windows_are_missing`, `test_missing_outcomes_gate` |
| Treatment contamination simulation | simulator `contamination_fraction` (`test_contamination_charges_control_units_the_treatment_price_and_logs_it`), `test_contamination_is_detected_and_measured`, `test_iv_recovers_elasticity_under_contamination_and_itt_is_diluted`, contamination world (seed 42 below), `test_contaminated_world_is_flagged_on_exactly_the_contaminated_tests` (dbt marts) |
| Pre-treatment balance | `test_balance_passes_under_randomisation_and_fails_on_selection` (negative control: selection on pre-period demand) |
| Bayesian: convergence, ESS, divergences | `test_every_diagnostic_is_computed_and_gated`, `test_gate_logic` (incl. NaN R-hat), `test_too_small_a_sampler_budget_fails_the_gate` (sampling completes, gate refuses) |
| Bayesian: posterior predictive checks | `test_posterior_predictive_detects_a_misfit`; 4 PPC statistics gated |
| Bayesian: prior sensitivity | "wide" and "narrow" refits, shift in posterior SD gated at 0.5 |
| Do not accept a model merely because sampling completed | artifact refused unless every gate passes (`test_failed_analysis_cannot_become_an_artifact`, `test_cli_refuses_to_publish_a_failed_analysis`) |

Other: hash neutrality of the simulator change (existing golden checksum and pinned config hashes
unchanged, `test_contamination_field_is_hash_neutral_at_zero`); registry vs world agreement; artifact
round trip, tamper detection, clock-independent version; Bayesian fit reproducible for a fixed seed.

## Results: evaluation world (seed 42, 8,000 customers, 1,513,024 events)

Units: 16,584 eligible (test, customer) units, 16,299 with an observed outcome (1.7% missing), all
16,299 in the hierarchical model (18 cells, smallest above the 50-unit floor). `data_version`
`units-efcc3bdae7231916`.

### Experiment validity (all gates passed)

| Test | Eligible | Control / treatment | SRM p | Balance p (max abs SMD) | Missing T / C | Assign. / exposure / contamination |
| --- | --- | --- | --- | --- | --- | --- |
| api_requests +20% | 4,124 | 2,041 / 2,083 | 0.51 | 0.84 (0.06) | 1.5% / 1.7% | 0 / 0 / 0 |
| cpu_minutes x1/1.2 | 4,696 | 2,381 / 2,315 | 0.34 | 0.77 (0.04) | 1.9% / 1.5% | 0 / 0 / 0 |
| gpu_minutes +20% | 1,334 | 671 / 663 | 0.83 | 0.99 (0.05) | 2.1% / 1.6% | 0 / 0 / 0 |
| data_transfer x1/1.2 | 4,332 | 2,131 / 2,201 | 0.29 | 0.72 (0.04) | 1.9% / 2.2% | 0 / 0 / 0 |
| premium_latency +20% | 2,098 | 1,072 / 1,026 | 0.32 | 0.12 (0.10, industry=fintech flagged) | 1.6% / 1.0% | 0 / 0 / 0 |

Reported (not gated): late-arrival SRM p 0.13-0.67 (no detectable selection at this size); factorial
interference after the fix: every cross-price and interaction p >= 0.21; guardrails: churn difference
(T - C) +0.05 to +1.2 pp, revenue per active day ratio (T / C) 1.085, 1.034, 0.938, 0.861, 0.835.

### Ground-truth recovery (`praxis.science`, all 8 pre-registered checks passed)

| Check | Result | Threshold |
| --- | --- | --- |
| Sign | pooled CI high -1.296; tier 95% highs -0.609 / -1.159 / -1.812 | all < 0 |
| Pooled magnitude (log-log OLS) | **-1.3335 (SE 0.0192) vs truth -1.3315**: 0.15% error, z = 0.10 | <= 10%, abs(z) <= 3 |
| Tier magnitude (hierarchical) | enterprise -0.674 vs -0.710 (5.1%); growth -1.210 vs -1.201 (0.7%); starter -1.875 vs -1.862 (0.7%) | <= 25% |
| Segment ordering | 14 identifiable pairs (3 tier, 11 industry), all correct, P(correct) >= 0.9997 | P >= 0.95, >= 1 pair |
| Cell 90% interval coverage | 17 / 18 (0.94) | >= 0.70 |
| Pooling justified | cell RMSE vs truth: hierarchical 0.0668, empirical Bayes 0.0678, unpooled 0.0717 | hier <= unpooled |
| Validity gates | none failed | all pass |
| Bayesian diagnostics | R-hat max 1.0017, ESS bulk min 2,242, tail min 3,199, 0 divergences; PPC p 0.39 / 0.43 / 0.57 / 0.86; prior shift max 0.21 SD | see gates |

Segment estimates (posterior mean, SD): tiers enterprise -0.674 (0.033), growth -1.210 (0.027),
starter -1.875 (0.032); industries health -0.973, fintech -1.045, saas -1.225, ecommerce -1.573,
media -1.584, gaming -1.599 (SD 0.034-0.058). Unpooled OLS tiers agree within 0.004.

### Failure cases measured (reported)

| Estimator | api | cpu | gpu | data_transfer | premium |
| --- | --- | --- | --- | --- | --- |
| Randomised (ITT) | -1.244 | -1.381 | -1.264 | -1.453 | -1.202 |
| Naive pre/post (treated only) | -0.970 | -1.669 | -0.936 | -1.708 | -0.934 |

The naive estimate is biased by the common demand trend divided by the log ratio, upward for raises
and downward for cuts. Dose: raises -1.236 (truth -1.210), cuts -1.415 (truth -1.434): the gap is
composition, not curvature.

### Contamination world (seed 42; 20% of control on api_requests and data_transfer_gb)

| Check | Result | Threshold |
| --- | --- | --- |
| Detected contamination | api 0.200, data_transfer 0.189 (configured 0.20) | within 3 pp |
| IV pooled | -1.338 vs truth -1.332 (0.5% error); ITT pooled -1.205 | <= 15% |
| ITT attenuated | api ITT -1.012 vs IV -1.264; data_transfer -1.192 vs -1.471 (first stage 0.80 / 0.81) | abs(ITT) < abs(IV) |
| Other gates | only the two `contamination` gates failed, as designed; no artifact saved | - |

### Development world (seed 1) with the final method

Pooled -1.355 vs truth -1.337 (1.3%, z 0.91); tiers 8.1% / 2.2% / 2.0%; 14 identifiable pairs all
correct; cell coverage 16 / 18; RMSE hierarchical 0.074 vs unpooled 0.090. Contamination dev world:
detection 0.191 / 0.189, IV -1.356 vs -1.337 (1.4%).

## Performance metrics

8,000 customers x 56 days: simulate + raw load + dbt ~50 s (1.51 M events); analysis (three PyMC fits
incl. prior sensitivity, 4 x 2,000 draws each) ~32 s; evaluation < 2 s. Peak RSS of the simulator
~300 MB. `nightly-science` adds ~4 min (dev world + contamination world).

## Failures found and fixes

* Bayesian divergence gate failed on dev with the registered parameterisation -> amendment (ADR 0012).
* Interference diagnostic confounded by product mix -> centred on the design mean (regression test).
* `_census` assumed a non-empty exposure tuple -> guarded (test).
* `praxis.science` CLI expected a `days` key that the simulator manifest does not write -> uses the
  scenario's own run length (the config-hash check guards any mismatch).

## Known limitations

* Elasticity is identified only within the tested range (abs(log ratio) = 0.18); the simulator's elasticity
  is constant by construction, so curvature is untested.
* Outcome is the intensive margin (demand per active day). Churn and conversion responses to price are
  guardrail metrics here and belong to Phases 6 and 9.
* Two-stage hierarchical model treats cell SEs as known (cells average ~900 units).
* Interference is checked only between concurrent tests on the same customers; no cross-customer
  spillover test exists (shared regional capacity is common to both arms in this world).
* `premium_latency` balance flagged industry = fintech at SMD 0.104 (chance at n = 2,098; omnibus
  p 0.12). Not adjusted: delta differences out unit levels.
* Synthetic only. No real price test has been run.

## Cloud cost incurred

GBP 0. Local DuckDB, dbt and PyMC only.

## Gate

PASS

## Reason

Every required_test.md s11 item has a test. On the held-out evaluation world (run once with the final
method) every experiment-validity gate, every Bayesian diagnostic gate and all 8 pre-registered
recovery checks passed. The pooled log-log elasticity is within 0.15% of truth, tier elasticities
within 5.1%, all identifiable segment orderings are recovered, and 17 of 18 cell intervals cover. The
contamination world is detected, ITT is diluted as predicted, and IV recovers the truth within 0.5%.
Failure cases (no control group, contamination, low counts, composition, unidentified variances) are
measured and documented. The one post-run change fixed a reported diagnostic and left every gated
number byte-identical.
