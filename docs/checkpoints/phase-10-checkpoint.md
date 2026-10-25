# Phase 10 checkpoint: resume here (written 2026-10-17; ATTEMPT 2 state added 2026-10-19)

## 0. ATTEMPT 2 STATE AT PAUSE (2026-10-19 ~02:00) -- READ THIS FIRST

The owner (2026-10-18) delegated authority to redesign Phase 10 requirements where
technically justified (no weakened safety, no faked results), decide independently, then
complete Phase 10, verify and report. Never commit or push. Seed 621 is evaluated ONCE.

### Done (all uncommitted in the working tree; `git status` = data/mlops/attempt2/git-status-at-pause.txt)
1. **Diagnosis (dev seed 1).** Arrivals-stop hypothesis REFUTED (continued arrivals decay
   identically). Cause CONFIRMED: compounding heterogeneous per-customer growth -> demand
   concentration (frozen champion 1.41x / eu_west 2.43x; naive also 1.38x). With growth off
   the frozen champion stays 0.81-0.96x, slices <= 1.20x, zero breaches. Skill-normalised and
   bias-based signals measured and REJECTED (both hide the drift ramp). Evidence files:
   `data/mlops/attempt2/diag-*.txt`. Full write-up: ADR 0017 section "Attempt 2".
2. **Steady world** `configs/simulator/scenarios/mlops_steady.toml` (NEW) = pricing_shadow +
   `growth_mean = 0.0`, `growth_sd = 0.0001`. `mlops_drift.toml` rebased on it (verified:
   identical to the committed drift world except `[population]`). pricing_shadow untouched.
   Makefile `mlops-data` uses mlops_steady. Tests in tests/mlops/test_config.py check both copies.
3. **Persistence:** `[monitoring] persistence_days` (currently 3 = PROVISIONAL, see below);
   `monitoring.persistent()`, `drift_check(history=...)` reports `breaches` and `alarms`;
   triggers use alarms; `pipeline._breach_history` reads the champion's consecutive reports.
4. **Repeated promotion:** `drill._operate` / `_approve_ready`: operator approves every
   READY version in steady AND drift worlds; new checks helpers `_moves_are_authorised`,
   `_final_champion_is_approved`; detection = first alarm on/after onset.
5. **Same-origin comparison:** `validation.check_champion_skill(backtest, art, champion,
   data, cfg, gate)`: same recipe hash -> 1.0 (identical backtest), else re-run champion
   recipe on candidate's panel; fail closed if folds differ.
6. **Speed:** worlds in spawned processes (`DrillRegistries` picklable factory in drill.py;
   threads segfault LightGBM), per-world candidates dirs + MLflow files,
   `OMP_WAIT_POLICY=PASSIVE` in `make mlops-drill`. ~1h53m per seed with 2 seeds at once.
7. Docs partly updated: ADR 0017 "Attempt 2" section, docs/mlops.md, lifecycle_acceptance.toml
   text (worlds: dev [1,611,612,613], eval 621; thresholds UNCHANGED).
8. `tests/mlops`: 124 pass; only the 3 hash-pin/copy tests failed then (copy test now fixed;
   pins MUST be updated at step B). lint, mypy (353 files), import-linter all clean.

### Dev calibration so far (data/mlops/attempt2/calibration-seeds-1-611.txt)
- Seeds 1 and 611: 12/12 checks PASS. Steady: 0 breach days, max residual 0.97, max slice
  1.21; 2 approved promotions each, no rollback. Drift: first breach 08-06, alarm (k=3)
  08-08, deadline 08-10 (k=2 -> 08-07, k=4 -> 08-09). Bad recipe same-origin ratio 1.057
  (seed 1), 1.154 (611) vs limit 1.02 -> rejected; all 20 good retrains exactly 1.0.
  Drift world: probation auto-restored the previous champion on 09-13 on both seeds (the
  08-31 promotion was 1.09x / 1.06x worse in the accelerating ramp) -- safety net worked;
  the continuing ramp keeps alarms on to the end (documented, not gated).
- Seeds 612, 613: worlds BUILT (`data/mlops/seed61{2,3}-c1000/{steady,drift}`, new scenarios);
  drills were STOPPED at the pause (incomplete; rerun from scratch).

### UPDATE 2026-10-24 (resumed session)
- Step A DONE: 612 and 613 drills 12/12 PASS (k=3). Summary of all four:
  `data/mlops/attempt2/calibration-seeds-1-611-612-613.txt`; k=3 reports archived in
  `data/mlops/attempt2/k3/`.
- Step B decision DONE by the pre-written rule `data/mlops/attempt2/persistence-decision-rule.txt`:
  k=3 alarmed ON the deadline (08-10) on 612/613 -> infeasible; `persistence_days = 2` set in
  demand.toml (NOT yet re-pinned). ADR 0017 "Calibration" subsection written; placeholder
  CONFIRMATION_RESULT awaits the k=2 confirmation drills of seeds 1 and 612 (running).
- PAUSED 2026-10-24 (owner shut down the PC): the k=2 confirmation drills (seeds 1, 612)
  were STOPPED incomplete; their drill dirs are partial, so rerun both from scratch:
  `make pg-up`, then
  `sh data/mlops/attempt2/scripts/drill_dev.sh 1 & sh data/mlops/attempt2/scripts/drill_dev.sh 612 &`
  (~2 h; seed 1's k=3 report is safe in `data/mlops/attempt2/k3/` and
  `data/mlops/attempt2/dev-seed1-drill.json`).
- Then: `.venv/bin/python data/mlops/attempt2/scripts/calib.py 1 612` and apply the 12 checks.
  If both pass: fill CONFIRMATION_RESULT in ADR 0017, change "PENDING" in the demand.toml
  comment to the result, re-pin hashes in tests/mlops/test_config.py PINNED (demand.toml,
  lifecycle_acceptance.toml, mlops_drift.toml, ADD mlops_steady.toml), then steps C-E.
  If either fails: STOP, no held-out run, report.
- Verified this session: `tests/mlops` 124 pass (3 hash-pin tests deselected, expected to
  fail until re-pinned). Seeds 611-613 / 621 used by no other phase. Seed 621 NOT built.

### UPDATE 2026-10-25
- k=2 confirmation drills seeds 1 and 612 rerun from scratch: both 12/12 PASS (first alarm
  08-07 / 08-09; 0 steady alarm days). Reports `data/mlops/attempt2/k2/`, summary
  `data/mlops/attempt2/calibration-k2-confirmation-seeds-1-612.txt`. (Top-level
  `data/mlops/attempt2/dev-seed*-drill.json` are the k=3 runs, identical to `k3/`.)
- CONFIRMATION_RESULT filled in ADR 0017; demand.toml comment updated; PINNED re-pinned
  (4 files incl. mlops_steady.toml). Step C (`make check`) started; log
  `data/mlops/attempt2/make-check.log`. Seed 621 NOT built yet.
- `make check` attempts (each failure fixed at its cause; nothing skipped or loosened). All
  three fixes are DISCLOSED AMENDMENTS for docs/evidence/phase-10.md:
  1. exit 2, 1 test failed: `tests/mlops/test_monitoring.py::test_injected_level_shift...`
     "gap" fixture `[[r,s], []]` (history is most-recent-first) only breaks a run for k=3.
     Generalised: a gap at EVERY position inside the k window must give no alarm (stronger).
     No monitoring code changed. `tests/mlops` 128 pass.
  2. exit 2, coverage-gate: `src/praxis/mlops/drill.py` 69% < 95% because attempt 2's
     parallel worlds run in spawned processes coverage did not measure. Added
     `patch = ["subprocess"]` to `[tool.coverage.run]` in pyproject.toml (pytest-cov 7 dropped
     subprocess measurement). drill.py -> 99.2%; then "coverage floors OK (174 files)",
     1657 passed.
  3. exit 2, `audit`: new external advisories. `uv lock --upgrade-package` certifi,
     opentelemetry-api (+sdk, semconv lockstep 1.45.1), filelock 4.2.4, gitpython 3.2.1,
     setuptools 84.2.1, virtualenv 21.14.13; `uv sync --locked`; pip-audit clean.
     frontend `npm audit fix`: next 16.3.8 -> 16.4.3 (critical RCE GHSA-2464-fhr4-w9xw; in
     ^16.3.8 range; only package-lock.json changed); npm audit 0 vulns. Old lockfiles in
     `data/mlops/attempt2/lock-backups/`. No modelling dependency changed.
  4. 4th run STOPPED incomplete 2026-10-25 (owner left). NEXT: `make pg-up`, `make check`
     (~20 min) -> must exit 0 (lint/typecheck/frontend/terraform not yet re-verified after the
     upgrades). Then step D (seed 621 ONCE), step E. State snapshot:
     `data/mlops/attempt2/git-status-at-pause3.txt`, `config-hashes-at-pause3.txt`.
  5. 5th run 2026-10-25 17:09-17:29: **exit 0** (1657 passed, 2 skipped opt-in live suites;
     coverage floors OK 174 files; pip-audit / npm audit clean; lint, mypy, arch, frontend,
     terraform, workflow-lint green). Log `data/mlops/attempt2/make-check-run5.log`. Code
     frozen: `data/mlops/attempt2/code-freeze-before-621.txt` (hashes of src, configs,
     Makefile, pyproject, uv.lock).
- STEP D STARTED 2026-10-25 ~17:30: `make mlops-data ML_SEED=621` + `make mlops-drill
  ML_SEED=621` (logs `data/mlops/seed621_build.log`, `seed621_drill_make.log`, status lines
  in `data/mlops/drill_v2.status`). **Seed 621 is now SPENT. Never run it again**, even if
  this run was interrupted: an interrupted held-out run is reported as such, not re-rolled.
- nightly-security 2026-10-25: exit 2 at gitleaks, the SAME known false positive only
  (`generic-api-key`, `tests/payments/test_ids_and_model.py:44`, commit 105c930, Stripe
  lookup key literal; not allowlisted); pip-audit + npm audit clean; trivy standalone exit 0.
- Done: CLAUDE.md seed list (601, 621 held-out; 611-613 dev), docs/mlops.md k = 2, dev
  reports copied to `docs/evidence/phase-10-attempt2-dev-seed*-{k2,k3}-drill.json`.
- **HELD-OUT SEED 621 (run once, 2026-10-25, build 59 s, drill 6011 s): 12 / 12 PASS.**
  Steady 0 breach / 0 alarm days; drift first alarm 08-09 (1 day slack); bad recipe rejected
  (1.142); rollback 08-23 (1.082); lineage 16. Report
  `docs/evidence/phase-10-attempt2-eval-seed621-drill.json`; summary
  `data/mlops/attempt2/heldout-seed621-summary.txt`.
- nightly-science 2026-10-25: exit 0 (only known Phase 8 `champion_truth_recovery`).
- STEP E DONE: docs/evidence/phase-10.md (attempt 2 PASS on top, attempt 1 FAIL kept),
  ADR 0017 (held-out result), docs/mlops.md, project_progress.md. **Phase 10 attempt 2
  COMPLETE; nothing committed.** Open owner decisions: Phases 6 / 9 re-evaluation after the
  Phase 4 loader fix; gitleaks false-positive allowlist. Do not start Phase 11 unasked.
- 2026-10-25 owner chose option A: Phase 4 loader defect disclosed in Phase 6 / 9 evidence,
  no re-evaluation. Remaining open: gitleaks allowlist.

### Remaining steps (in order)
A. Rerun dev drills 612 and 613 (both at once, ~2 h):
   `sh data/mlops/attempt2/scripts/drill_dev.sh 612 & sh data/mlops/attempt2/scripts/drill_dev.sh 613 &`
   (needs `make pg-up`; status lines go to data/mlops/drill_v2.status), then
   `.venv/bin/python data/mlops/attempt2/scripts/calib.py 1 611 612 613`.
B. Decide persistence_days from all four dev seeds (leaning k=2 unless 612/613 show
   single-day steady breaches: k=3 leaves only 2 days of detection slack). Write the
   calibration table into ADR 0017 ("Attempt 2" -> add "Calibration" subsection) and the
   demand.toml comment; re-pin hashes in tests/mlops/test_config.py PINNED (demand.toml,
   lifecycle_acceptance.toml, mlops_drift.toml, ADD mlops_steady.toml).
C. `make check` must exit 0 BEFORE the held-out run (code frozen after this).
D. `make mlops-data ML_SEED=621` then `make mlops-drill ML_SEED=621` ONCE; report as measured.
   Copy dev + eval drill.json to docs/evidence/phase-10-attempt2-*.json.
E. `make nightly-science`, `make nightly-security`. Add 621 (and dev 611-613) to the CLAUDE.md
   held-out list. Update docs/evidence/phase-10.md (keep attempt-1 FAIL; add attempt 2 per
   required_test.md s23; disclose every amendment), docs/mlops.md, ADR 0017,
   project_progress.md, this checkpoint. Final report to owner incl. open decisions
   (Phases 6/9 re-evaluation after the loader fix; gitleaks false-positive allowlist).

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
