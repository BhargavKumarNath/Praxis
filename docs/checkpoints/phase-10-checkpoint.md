# Phase 10 checkpoint: resume here (written 2026-10-17)

Status: Phase 10 attempt 1 is CLOSED with gate **FAIL** (evidence: `docs/evidence/phase-10.md`).
Owner decision 2026-10-17: **fix Phase 10 properly before Phase 11** (do not start Phase 11).
Nothing is committed or pushed. Never push. Never commit unless the owner explicitly asks.

Read next, in order: this file, `CLAUDE.md`, `docs/evidence/phase-10.md`,
`docs/adr/0017-model-registry-continuous-training-and-safe-promotion.md`, `docs/mlops.md`,
`configs/mlops/demand.toml`, `configs/mlops/lifecycle_acceptance.toml`, the Phase 10 sections
of `project_plan.md` (gate) and `required_test.md` (s16, s23 template), and the end of
`project_progress.md` ("Phase 10 results", "Carry-over (Phase 10)", "Phase 10 working log").

---

## 1. What exists and works (do not rebuild)

All uncommitted, in the working tree (`git status`):
- `src/praxis/domain/model_lifecycle.py`: stage machine (candidate, challenger, shadow,
  canary, champion, retired, rejected, rolled_back) + retraining-request machine.
- `src/praxis/control/migrations/versions/0005_model_registry.py`: `model_versions`
  (one champion / shadow / canary by partial unique index, stage guard trigger),
  `model_stage_events` (append-only audit with decision_id), `model_validation_reports`,
  `model_monitoring_reports`, `retraining_requests` (one live per model).
- `src/praxis/mlops/`: `config`, `store`, `registry` (approval policy, bootstrap,
  dev-only `force_promote`, rollback), `lineage`, `tracking` (MLflow mirror; Postgres is
  authoritative), `scoring`, `drift`, `training`, `validation` (16 checks), `monitoring`,
  `shadow`, `serving` (`RegistryForecaster`), `pipeline` (`DemandLifecycle.run_day`),
  `drill`, `__main__` (CLI; `drill`, `status`, ...).
- API: `PRAXIS_FORECAST_FROM_REGISTRY`, `X-Praxis-Model-Version` header, `/model` shows stage.
- `pyproject.toml`: `praxis.mlops` in layer `pricing | payments | mlops`; in "models never
  import the simulator" and "no web framework" contracts; coverage floors mlops/* 95
  (CLI 85). `mlflow-skinny` 3.16.1 in `uv.lock`.
- Makefile: `mlops-data` (build steady + drift worlds), `mlops-drill` (`ML_SEED`,
  `ML_CUSTOMERS`, `ML_DIR`). Drill `--start-date` default 2026-01-05 (world day 0).
- Tests: `tests/mlops/` 121 pass (toy-world drill is marked slow). Needs
  `PRAXIS_TEST_DATABASE_URL=postgresql+psycopg://praxis@127.0.0.1:55432/postgres` and
  `make pg-up` when run outside `make test`.
- Phase 4 defect FIXED: `RidgeBaseline.from_state` rebuilt one-hot blocks in sorted key
  order (every LOADED artifact misapplied categorical coefficients). Dev seed 1 vWAPE
  0.1623 buggy vs 0.1558 fixed. Regression test + `serving_parity` gate check.

Verified on the final code (2026-10-17): `make check` exit 0 (1650 passed, 2 skipped =
opt-in live BigQuery / Stripe, coverage floors OK, 174 files). `make nightly-science`
exit 0 (only known Phase 8 failure `champion_truth_recovery`). `make nightly-security`
fails ONLY at gitleaks on the Phase 8 false positive (see s6); trivy / pip-audit / npm
audit clean.

## 2. Results of attempt 1

Pre-registered drill acceptance = 12 checks (`configs/mlops/lifecycle_acceptance.toml`).

| Check | Dev seed 1 (before amendment) | Held-out seed 601 (after amendment, run once) |
| --- | --- | --- |
| champion_validation_passes | PASS | PASS |
| bad_candidate_rejected | FAIL | PASS |
| steady_candidate_reaches_ready | PASS | PASS |
| steady_candidate_promoted_by_approver | PASS | PASS |
| drift_detected_within_days (14) | PASS* | PASS* |
| drift_opens_retraining_request | PASS | PASS |
| steady_max_decision_alarm_days (0) | FAIL (13/57) | FAIL (30/57) |
| no_champion_change_without_approver | PASS | PASS |
| rollback_drill | PASS (ratio 1.075) | PASS (ratio 1.382) |
| lineage_complete_for_every_version | PASS (19) | PASS (21) |
| served_version_equals_registry_champion | PASS | PASS |
| serving_stages_hold_compatible_contracts | PASS | PASS |

\* first detection was a signal the steady world also fires (dev 07-27 eu_central noise;
601 07-25, before onset 07-27). Dev drift-caused detection 08-07 (onset + 11).

Reports: `docs/evidence/phase-10-dev-seed1-drill.json`,
`docs/evidence/phase-10-eval-seed601-drill.json`.

### Root causes (measured, see the evidence report for numbers)
1. **Steady false alarms.** (a) The "steady" world (`pricing_shadow.toml`) sets
   `arrival_horizon_days = 196`: new customers stop arriving at day 196 while churn
   continues, so a frozen model's error rises relative to its backtest reference (dev: up to
   1.41 overall / 2.43 eu_west; 601: up to 1.44 overall / 2.37 us_east). HYPOTHESIS, not yet
   isolated: confirm on a dev world (demand level per region after day 196, steady vs a
   continued-arrivals world) before relying on it. (b) Single noisy days just over the slice
   limit (dev eu_central 1.52-1.53 vs 1.50 on 3 days). (c) The drill approves only the
   FIRST READY version (pre-registered), so the champion ages while newer retrains pass
   shadow. (d) Criterion "0 alarm days" was never measured on any dev world.
2. **Bad candidate passed (dev).** The gate had only absolute Phase 4 acceptance. The bad
   recipe (ridge alpha 1e9, 1 boosting round) scored skill 0.834 vs champion 0.775 (limit
   0.90). Amended 2026-10-17 (owner-approved, ADR 0017): `champion_non_inferiority`
   (candidate skill <= 1.02 x champion skill; skill = vWAPE / seasonal-naive vWAPE).
3. **Amendment side effect (601).** `champion_non_inferiority` compares each version's skill
   from ITS OWN backtest window. Drift-world retrains' backtests include the harder drifted
   weeks, the champion's do not: 4 of 8 drift retrains rejected (ratio 1.027-1.044) while the
   champion was 1.6-1.8 x worse than its reference on those weeks.
4. Underlying process problem: acceptance criteria were pre-registered from assumptions,
   never calibrated on development worlds, so the first held-out run discovered them.

## 3. Seeds (NEVER re-run a spent held-out seed)

- Spent held-out seeds (whole project): 42, 43, 44, 45, 201-203, 301-303, **601** (Phase 10
  attempt 1). Phase 9 development worlds used seeds 101-124. Seed 1 = development everywhere.
- Phase 10 attempt 2 proposal (pre-register in the new acceptance file BEFORE building):
  development / calibration seeds **611, 612, 613** (+ seed 1); fresh held-out seed **621**,
  built only after the criteria are pinned, run ONCE. Check no other phase used them
  (`grep -rn "seed 61\|seed 62" docs project_progress.md CLAUDE.md`).

## 4. Pinned files (hash in tests; edit only with ADR + owner approval)
- `configs/mlops/demand.toml` (re-pinned 2026-10-17 for `max_skill_ratio_vs_champion`),
  `configs/mlops/lifecycle_acceptance.toml`, `configs/simulator/scenarios/mlops_drift.toml`
  -> `tests/mlops/test_config.py::PINNED`.
- `configs/simulator/scenarios/pricing_shadow.toml` is pinned by `tests/pricing/test_config.py`
  and used by Phases 6/9: do NOT edit it. A corrected steady world must be a NEW scenario
  file (e.g. `configs/simulator/scenarios/mlops_steady.toml`) used by `mlops-data`.
- The owner approved "fix Phase 10 properly" (2026-10-17): new / changed Phase 10 criteria
  are allowed when calibrated on development seeds, written in an ADR 0017 amendment, and
  pinned BEFORE the fresh held-out run. The attempt-1 FAIL stays in the evidence history.

## 5. Plan for attempt 2 (owner-approved direction; implement in this order)

Rules: calibrate on dev seeds only; pin criteria; then ONE fresh held-out run. Never tune on
601 or 621 results. Do not weaken safety properties (approval for champion, restore-only
automatic rollback, lineage, served = champion, bad candidate rejected).

1. **Diagnose first (cheap, dev seed 1 data already built).** Confirm the steady-world decay
   cause: demand / active customers per region over days 150-252 in
   `data/mlops/seed1-c1000/steady/warehouse.duckdb`; compare with a continued-arrivals world.
2. **Steady world.** New `mlops_steady.toml` = pricing_shadow world with arrivals continuing
   through the run (`arrival_horizon_days` = run days or unset). Keep the stress events.
   Rebase `mlops_drift.toml` on it so steady and drift differ ONLY by the injected drift
   (re-pin both; ADR). Note: changing the horizon also re-spreads arrivals before day 196.
3. **Persistence-aware monitoring.** Decision signals (residual / segment / calibration)
   raise an ALARM only after k consecutive evaluated days over threshold (k pre-registered
   in `[monitoring]`, e.g. `persistence_days`); single-day breaches are reported as
   observations. Retraining requests still open on schedule / new labels. Real drift must
   still be detected within the deadline (onset + 14 days).
4. **Repeated safe promotion.** Drill operator approves every version the registry reports
   READY (named approver each time, audit recorded), not only the first. Probation applies
   to each promotion; automatic moves remain restore-only.
5. **Same-date comparison.** `champion_non_inferiority` scores champion and candidate on the
   SAME rolling origins (e.g. re-run the champion's recipe/artifact on the candidate's
   backtest origins, or score both artifacts on the candidate backtest's matured origins
   after the champion's training end; fall back to "insufficient overlap" -> rely on shadow).
   Must keep rejecting the bad recipe; must not reject same-recipe retrains on drifted data.
6. **Speed.** Share the bootstrap champion and the bad-recipe artifacts across the three
   registries (train once per world); cache warehouse loads; parallel seed runs (16 cores,
   14 GB RAM; ~0.5 GB per drill process); optional shorter dev runs for calibration of
   monitoring only. Drill attempt 1 took ~2.5 h per seed (~1 min per lifecycle day).
7. **Calibrate** on dev seeds 1, 611-613: measure steady-world alarm days, drift detection
   lag, good-vs-bad skill ratios (same dates), retrain acceptance rate. Derive each limit
   with a written margin; record the measurements in the ADR amendment.
8. **Pre-register + pin** the new `lifecycle_acceptance.toml` (and `demand.toml` changes),
   build seed 621 worlds, run the drill ONCE, report as is.
9. `make check` (exit 0), `make nightly-science`, `make nightly-security`; update
   `docs/evidence/phase-10.md` (keep attempt 1 as history), ADR 0017, `docs/mlops.md`,
   `project_progress.md`, `CLAUDE.md` (spent seeds). Stop for owner review.

## 6. Open owner decisions (remind the owner at the end)
- Do Phases 6/9 need fresh pre-registered evaluations because of the Phase 4 loader defect?
- Gitleaks false positive: `generic-api-key`, `tests/payments/test_ids_and_model.py:44`,
  commit 105c930, fingerprint
  `105c9306cdbe7e74f2b77a5183bbd9d77371c95e:tests/payments/test_ids_and_model.py:generic-api-key:44`
  (Stripe lookup key literal; allowlist by fingerprint only with owner approval).

## 7. Commands
```bash
make pg-up                                   # local Postgres (Docker praxis-pg, port 55432)
export PRAXIS_TEST_DATABASE_URL=postgresql+psycopg://praxis@127.0.0.1:55432/postgres
.venv/bin/pytest tests/mlops -q              # ~5 min incl. slow toy drill
make mlops-data ML_SEED=<seed>               # build steady + drift worlds (~10 min)
make mlops-drill ML_SEED=<seed>              # ~2.5 h; writes data/mlops/seed<seed>-c1000/drill/
make check                                   # ~21 min; must exit 0
make nightly-science && make nightly-security
```
Note: `make mlops-drill` recreates its `praxis_mlops_drill_*_s<seed>` databases and
overwrites `data/mlops/seed<seed>-c1000/drill/` and `drill.log`.

## 8. Preserved evidence (attempt 1)
- `docs/evidence/phase-10.md`, `phase-10-dev-seed1-drill.json`,
  `phase-10-eval-seed601-drill.json` (tracked).
- Local archive (gitignored `data/`): `data/mlops/archive/2026-10-17-phase10-v1/`
  - `seed1/`, `seed601/`: full drill outputs (models, MLflow db, drill.json) + drill.log
  - `logs/`: make check (x2), nightly-science, nightly-security, build and drill make logs
  - `pg/`: `pg_dump -Fc` of the six registry databases (`pg_restore` to inspect)
  - `SHA256SUMS`
- Built worlds: `data/mlops/seed1-c1000/{steady,drift}`, `data/mlops/seed601-c1000/{...}`
  (601 is spent: never run on it again).
