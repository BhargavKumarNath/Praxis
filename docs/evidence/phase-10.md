# Phase 10 Evidence Report

```text
Phase: 10 - MLOps, Continuous Training, Drift, and Safe Promotion
Date: 2026-10-11 (build) - 2026-10-17 (drills, final)
Code revision: 12509c4 + working tree (uncommitted; no commits made by Claude)
Environment: Linux 7.0, Python 3.12.14, Postgres 17-alpine (local Docker), DuckDB + dbt,
             mlflow-skinny 3.16.1 (local SQLite mirror), Pub/Sub emulator (local Docker)
All customers, demand, models and drift are SYNTHETIC. No cloud resource was created or used.
```

Design: ADR 0017 (`docs/adr/0017-model-registry-continuous-training-and-safe-promotion.md`),
`docs/mlops.md`. Pre-registered, hash-pinned: `configs/mlops/demand.toml` (policy),
`configs/mlops/lifecycle_acceptance.toml` (drill acceptance),
`configs/simulator/scenarios/mlops_drift.toml` (drift world).
Raw reports: `phase-10-dev-seed1-drill.json` (development), `phase-10-eval-seed601-drill.json`
(held-out, run ONCE).

## Gate: FAIL (1 of 12 pre-registered drill checks, on both worlds)

Every project_plan gate item is demonstrated on the held-out world. The pre-registered drill
acceptance is not met: `steady_max_decision_alarm_days` (limit 0) failed on the development
world (13 / 57 days) and on the held-out world (30 / 57 days). That limit was not changed.

| project_plan gate | Held-out seed 601 | Dev seed 1 |
| --- | --- | --- |
| bad candidate is automatically rejected | PASS (rejected: `backtest_acceptance`, `champion_non_inferiority`) | FAIL before the amendment (entered shadow) |
| good candidate can progress through configured stages | PASS (retrain -> shadow -> READY -> approved champion 2026-08-10) | PASS |
| rollback restores previous known-good version | PASS (forced bad champion rolled back 2026-08-23, vWAPE ratio 1.382 > 1.05; router serves the previous version; audit holds the decision) | PASS (ratio 1.075) |
| drift alert can create retraining request | PASS (4 drift-triggered requests; see caveat on attribution) | PASS |
| registry records complete lineage | PASS (21 versions, nothing missing) | PASS (19) |
| production service exposes model version | PASS (served = registry champion in all three registries; `X-Praxis-Model-Version`) | PASS |

| Pre-registered drill check | Dev seed 1 | Held-out seed 601 |
| --- | --- | --- |
| champion_validation_passes | PASS | PASS |
| bad_candidate_rejected | **FAIL** | PASS |
| steady_candidate_reaches_ready | PASS | PASS |
| steady_candidate_promoted_by_approver | PASS | PASS |
| drift_detected_within_days (14) | PASS | PASS |
| drift_opens_retraining_request | PASS | PASS |
| steady_max_decision_alarm_days (0) | **FAIL** (13) | **FAIL** (30) |
| no_champion_change_without_approver | PASS | PASS |
| rollback_drill | PASS | PASS |
| lineage_complete_for_every_version | PASS | PASS |
| served_version_equals_registry_champion | PASS | PASS |
| serving_stages_hold_compatible_contracts | PASS | PASS |

## Tests executed
- `make check` (final, after the amendment): exit 0; 1650 passed, 2 skipped (opt-in live
  BigQuery / Stripe suites, by design); coverage floors OK (174 files; `mlops/*` 95%).
  Lint, mypy, import-linter contracts (mlops layer, "models never import the simulator"),
  schemas, secrets, audit, frontend, terraform, workflow-lint all green.
- `tests/mlops/` 121 tests (registry, store, lifecycle machine, validation incl. known-good /
  known-bad / tampered / leaky / non-reproducible candidates, shadow, monitoring, drift,
  retraining, probation, serving router, tracking mirror, toy-world drill).
- required_test.md s16: registry lineage fields; known-good vs known-bad gate; shadow on the
  same observations without affecting production; controlled bad champion -> rollback
  (pointer restored, service serves old version, audit records rollback, contracts
  compatible); injected drift -> detector fires -> retraining request -> no blind promotion.
- `make nightly-science`: exit 0 (only the known Phase 8 failure `champion_truth_recovery`).
- `make nightly-security`: FAIL at gitleaks on the false positive first surfaced in Phase 8
  (`generic-api-key`, `tests/payments/test_ids_and_model.py:44`, commit 105c930, the Stripe
  lookup-key literal `praxis_growth_gbp_4900_month`; not a credential, NOT allowlisted).
  pip-audit (incl. the new mlflow-skinny), npm audit and trivy (run standalone) clean.
- `make mlops-drill ML_SEED=1` (development) and `ML_SEED=601` (held-out, once).

## Results
- Held-out seed 601: 11 / 12 checks pass. Champion `demand-hybrid-25eed98d68eb`.
- Dev seed 1: 10 / 12 before the amendment (see Failures).

## Performance metrics
- Drill wall time ~2.5 h per seed (57 lifecycle days x 3 registries, 1000 customers; ~1 min
  per day incl. retraining, validation, shadow, monitoring).
- Validation, held-out seed 601, worst candidate: `latency_budget` p95 95.8 ms (budget
  500 ms); `memory_budget` heap peak 8.15 MB (budget 256 MB).

## Statistical metrics (SYNTHETIC)
- Backtest skill (vWAPE / seasonal naive): dev champion 0.775, dev bad recipe 0.834;
  held-out champion 0.789, held-out bad recipe 0.862 (under 0.90, but on 601 it also failed
  the absolute acceptance item `pinball_below_ridge`, so the original gate alone would have
  rejected it there).
- Rollback probation: dev 1.075, held-out 1.382 x the previous champion (limit 1.05).
- Steady world (no intervention after day 195), held-out: residual ratio up to 1.44, us_east
  slice up to 2.37 (07-25 .. 08-23); dev: eu_central 1.53 noise (3 days) and ageing of the
  promoted champion to 1.41 overall / 2.43 eu_west by 09-14.

## Failures found
1. **Dev `bad_candidate_rejected` (seed 1).** The pre-registered misconfigured recipe
   (ridge alpha 1e9, one boosting round) passed all 15 checks (0.834 <= 0.90) and entered
   shadow: the gate had only absolute acceptance, no comparison with the incumbent.
2. **`steady_max_decision_alarm_days` (dev 13, held-out 30; limit 0).** The "steady" world
   is not stationary for a frozen model: arrivals stop at day 196 while churn continues, so
   accuracy relative to the backtest reference decays; dev also shows 3 noise days just over
   the 1.50 slice limit. Retrained candidates kept passing shadow, but the drill (as
   pre-registered) approves only the first READY version, so the champion ages. Not amended:
   any new limit would be chosen from seen results.
3. **Drift attribution caveat.** `drift_detected_within_days` passes on both worlds, but the
   first detection is a signal the steady world also fires (dev 07-27 eu_central noise;
   held-out 07-25 residual, before onset 07-27). Dev drift-caused detection: 08-07 (onset +
   11). Held-out: the drift world diverges from steady from 07-28 and clearly from 08-04
   (us_east 1.96 vs 1.80, from the api_requests x0.5 shock), within the deadline, but the
   check itself cannot separate it from the steady-world alarms.
4. **Side effect of the amendment (held-out).** `champion_non_inferiority` rejected 4 of 8
   drift-world retrains (08-11 .. 09-01; skill 0.810-0.823 vs champion 0.789, ratio
   1.027-1.044). Cause: each version's skill is measured on its OWN backtest; the retrains'
   backtests include the harder drifted weeks, the champion's do not, so the comparison is
   not like-for-like (the champion was 1.6-1.8 x worse than its reference on those weeks).
   No pre-registered check depends on it and nothing was changed after seeing it.
5. **Phase 4 defect found and fixed.** `RidgeBaseline.from_state` rebuilt one-hot blocks
   in sorted JSON key order, so every LOADED demand artifact misapplied categorical
   coefficients (forecast API and the Phase 6 / 9 pricing inputs). Dev seed 1: vWAPE
   0.1623 buggy vs 0.1558 fixed. Spent held-out pricing seeds were NOT re-run.

## Fixes
- Phase 4 loader: blocks rebuilt in `CATEGORICAL` order; unknown categories refused;
  regression test compares predictions; new gate check `serving_parity`.
- Pre-run amendments (2026-10-11, toy fixture only, before any simulator world): PSI noise
  bound + min 5 per bin (log_scale dropped); retraining cooldown / decision-signal rule;
  probation by 7 matured origins over 21 days; gate re-evaluates the backtest against the
  pinned acceptance instead of the report's own verdict.
- Dev-world method bugs (no threshold change): no retraining request without new labels; a
  retrain reproducing an existing version completes as `duplicate` (first dev drill crashed
  on day 196 for this reason).
- **Post-dev amendment (2026-10-17, owner-approved, ADR 0017):** new gate check
  `champion_non_inferiority` (candidate skill <= 1.02 x champion skill; margin reused from
  `[shadow]`). Checked on the saved seed-1 candidates before the held-out run: all 14
  good-recipe versions pass (max 1.017), both bad-recipe versions fail (1.074, 1.076).
  Judged once on seed 601. The bad recipe and every drill threshold are unchanged.

## Known limitations
- The steady-world false-alarm criterion fails; the decision signals fire on model ageing
  and slice noise. Options for a later phase (need a new pre-registration and fresh seed):
  reference refresh at promotion, noise-aware slice bounds, or an approver who keeps
  promoting READY versions.
- `champion_non_inferiority` compares skills from different backtest windows. Follow-up:
  score champion and candidate on identical rolling origins.
- Canary is not used (loader refuses it); one champion / shadow per model by index.
- Drill evidence is one dev and one held-out world at 1000 customers.
- Phases 6 / 9 used the buggy loader; whether they need fresh pre-registered evaluations is
  the owner's decision (open).

## Cloud cost incurred
- GBP 0 (local Docker Postgres, local DuckDB, local MLflow SQLite).

## Gate
FAIL

## Reason
The subsystem meets every project_plan gate item on the held-out world, and the hard safety
properties held throughout (no automatic champion change except a probation restore; full
lineage; rollback; served = champion). The pre-registered acceptance also required zero
decision-signal alarm days on the steady world; that failed on both worlds (13 and 30 days)
and was not loosened. The bad-candidate check passes on the held-out world only after an
owner-approved post-development amendment, disclosed above with its side effect.
