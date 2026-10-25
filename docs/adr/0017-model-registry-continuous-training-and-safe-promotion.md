# ADR 0017: Model registry, continuous training, drift monitoring and safe promotion

Status: Accepted (Phase 10). Written 2026-10-11. Pre-registered and hash-pinned
(`tests/mlops/test_config.py`): the lifecycle policy `configs/mlops/demand.toml`, the drift
world `configs/simulator/scenarios/mlops_drift.toml` and the drill acceptance
`configs/mlops/lifecycle_acceptance.toml`. The policy was amended three times BEFORE any
simulator world was built or monitored (see "Pre-run amendments") and once AFTER the
development world, with the owner's approval (see "Post-development amendment"); every
amendment is written into the file it changed. Attempt 1 FAILED on held-out seed 601
(2026-10-17); attempt 2 (see "Attempt 2", owner-delegated redesign, also pins the steady
world `configs/simulator/scenarios/mlops_steady.toml`) PASSED 12 / 12 on fresh held-out
seed 621 (2026-10-25).

Extends ADR 0005 (retraining may be automatic; promotion is a separate, gated action).

## Context
Phases 4-9 each produce checksummed artifacts, but the system had no lifecycle: an artifact
directory was chosen by an environment variable, nothing recorded which version was live,
nothing watched a live model for drift, and nothing could undo a bad promotion. Phase 10
must make model lifecycle an actual running subsystem (project_plan Phase 10;
required_test.md s16) for the demand forecaster, the model the API serves.

## Decision

### Authoritative registry = the Postgres control plane (migration 0005)
`model_versions` (one row per artifact: data, feature and code versions, parameters,
metrics, checksum, training time, synthetic flag, monitoring reference; lineage columns are
immutable by trigger), `model_stage_events` (append-only audit; a promotion or rollback
writes both versions' moves under one `decision_id` in one transaction),
`model_validation_reports`, `model_monitoring_reports` (drift / shadow / probation; ids are
content hashes, so re-running a day is idempotent) and `retraining_requests` (at most one
live request per model, by partial unique index; ids are content hashes of (model, data date,
triggers)). Partial unique indexes make a second champion / shadow / canary
unrepresentable. Registration refuses incomplete lineage (`praxis.mlops.lineage`).

**MLflow is a mirror, not the source of truth.** `mlflow-skinny` 3.16.1 (SQLite tracking
store locally; no server, no cloud) records experiment, run, dataset reference (tags),
feature version, parameters, metrics, the artifact and the validation report, and one
registered model version per run; stage aliases (`champion`, `shadow`) are re-derived from
the registry after every decision (`sync_tracker`). A tracker failure is logged and never
changes or blocks a decision. Two writable sources of truth for "what is live" would
diverge; the control plane already holds every other operational state (ADR 0002).

### Lifecycle (`praxis.domain.model_lifecycle`)
`candidate -> challenger (validation passed) | rejected`, `challenger -> shadow`,
`shadow -> champion (promote) | rejected (shadow failed)`, `champion -> retired (replaced) |
rolled_back`, `retired -> champion (restore)`; `challenger -> champion` only by
`promote.bootstrap` (no champion exists). `promote.forced` (any pre-champion stage ->
champion) exists for development drills only: refused outside the policy's
`forced_promotion_environments` (never `prod`) and for the automation actor; audited. The
database trigger allows exactly the direct edges (not the closure: one audited step at a
time). `canary` exists in the machine but the policy loader REFUSES it: no canary evaluation
exists, and for a batch forecast consumed by a weekly pricing cycle, serving two versions to
a share of requests would mix versions inside one optimisation. Shadow already scores the
challenger on exactly the champion's observations.

### Who may move what
Automation may move a version up to `lifecycle.auto_advance_to = "shadow"`. Champion needs
a named approver (`champion_requires_approval`) and `ready_for_promotion`: the latest shadow
report passed AND compared against the CURRENT champion. The only automatic champion change
is a probation rollback, which restores the last known-good version and promotes nothing new.

### Offline validation gate (CANDIDATE -> CHALLENGER | REJECTED, automatic)
`artifact_integrity`, `lineage_complete`, `feature_contract` (serving code and champion),
`dataset_reference` (the warehouse reproduces the manifest `data_version`), leakage
(`leakage_training_window`; `leakage_future_invariance`: future days x10 change no forecast),
`backtest_acceptance` with `baseline_comparison`, `calibration_80/50`,
`segment_regression_vs_baseline`, `champion_non_inferiority` (amendment 2026-10-17) (the Phase 4 metrics RE-EVALUATED against the authoritative
pinned acceptance in `configs/forecast/demand.toml`, never the candidate's own `[acceptance]`
block or the report's verdict), `latency_budget`, `memory_budget`, `reproducibility`
(retrain -> identical checksum) and `serving_parity` (the LOADED artifact predicts exactly
like the freshly trained model; added after the defect below).

### Shadow, probation, drift, retraining
* **Shadow**: from the data day a version enters shadow, every forecast origin whose seven
  horizons have matured is scored once as a frame by both versions (same observations).
  Non-inferiority: pooled vWAPE and pinball <= 1.02 x champion, worst region/product/segment
  slice <= 1.10, interval coverage in band; >= 14 origins. Online, the registry-backed router
  runs the shadow version on the champion's snapshot on a background worker after the
  champion's answer is ready; callers only receive the champion's forecast.
* **Probation**: for 21 days after a promotion the replaced champion keeps being scored on the
  same matured origins; with >= 7 origins and vWAPE > 1.05 x the previous champion, the new
  champion is rolled back automatically.
* **Drift** (daily, trailing 14 days of matured out-of-sample forecasts): residual drift
  (pooled vWAPE > 1.30 x the champion's own backtest), segment drift (worst slice > 1.50 x
  its backtest slice), calibration drift (80% coverage outside [0.65, 0.95]) are DECISION
  signals: the champion is measurably worse. Feature / prediction PSI (decile bins fixed at
  registration; fires above 0.25 AND above the no-change sampling bound) are INVESTIGATION
  signals. Drift opens (or joins) a retraining request; it never promotes anything.
* **Retraining requests**: also on schedule (data age >= 28 days) and new labels (>= 1512
  matured targets). One live request per model; a 7-day cooldown; investigation-only
  triggers also wait while a retrained version is still in candidate / challenger / shadow,
  decision signals do not (that version may predate the change). A request trains the Phase 4
  recipe on everything observed, registers the artifact even when its backtest fails (a bad
  candidate must be rejected and audited, not dropped), validates it and moves a validated
  challenger into a free shadow slot.

### Serving
`RegistryForecaster` (`PRAXIS_FORECAST_FROM_REGISTRY`): re-reads the champion pointer at most
every `PRAXIS_MODEL_REFRESH_INTERVAL_S`; swaps a version in only after it loads, its checksum
equals the registered checksum and its feature contract equals the serving code's; otherwise
the last good version keeps serving (counted, logged). Every forecast response and
`/v1/forecasts/demand/model` name the served version (`X-Praxis-Model-Version`), stage and
shadow version.

### Layering
`praxis.mlops` sits beside `praxis.pricing | praxis.payments` (imports the model packages and
the control plane, never the simulator or `praxis.science`; no web framework).

## Pre-run amendments (2026-10-11, before any simulator world was built or monitored)
Found on the 12-series unit-test fixture while debugging the mechanics:
1. **Feature PSI fired on a world with no change.** PSI on ~40-80 distinct region-days is
   mostly sampling noise, and `log_scale` is a slowly moving per-series level (it identifies
   the series rather than describing an input distribution). Added the no-change bound
   chi2_{0.999}(B-1) x (1/n + 1/m) on effective sizes (distinct values), a validity floor of 5
   expected observations per bin, and removed `log_scale`. The 0.25 threshold is unchanged.
   Known limitation: rolling-window features are autocorrelated, so the bound is optimistic;
   PSI signals are therefore investigation-only (above).
2. **Daily re-requests while drift persisted.** Added the cooldown and the "under
   evaluation" rule; then refined it so decision signals are not suppressed by a candidate
   that predates the change (the fixture showed a 5-day delay).
3. **Probation decided on 1000 rows = one or two correlated days** and rolled back a good
   champion on the fixture. Evidence unit changed to fully matured origins (7); window 14 ->
   21 days so 7 origins can mature. The 1.05 margin is unchanged.
Also before any world: the validation gate trusted the backtest report's own verdict, which
used the candidate's config; it now re-evaluates the metrics against the pinned acceptance.

## Post-development amendment (2026-10-17, owner-approved; disclosed in phase-10 evidence)
The development drill (seed 1) FAILED `bad_candidate_rejected`: the pre-registered
misconfigured recipe (ridge alpha 1e9, one boosting round) cleared every absolute check
(vWAPE / seasonal naive 0.834 <= 0.90; champion 0.775) and entered shadow. The gate had no
comparison with the incumbent, so any recipe above the Phase 4 floor could displace a much
better champion in the queue. New check `champion_non_inferiority`: the candidate's backtest
skill (vWAPE / seasonal-naive vWAPE on its own rolling-origin backtest) must be at most
`[validation] max_skill_ratio_vs_champion = 1.02` x the champion's (from the champion's
manifest backtest summary; passes when there is no champion). The margin is the existing
shadow non-inferiority margin, not a value chosen from results. The candidate's backtest
panel extends the champion's, so the two skills are measured on largely the same origins.
Neither the bad recipe nor any drill threshold changed. It is judged once on the unused
evaluation seed 601. The other dev failure (`steady_max_decision_alarm_days`, 13 alarm days
vs 0) was NOT amended: it is reported as a failure (see phase-10 evidence).

## Attempt 2 (2026-10-18): amendments after the attempt-1 FAIL
Attempt 1 failed its pre-registered drill acceptance on held-out seed 601
(`steady_max_decision_alarm_days`: 30 alarm days vs a limit of 0); that result stands in the
evidence. The owner first approved "fix Phase 10 properly" (2026-10-17) and then, on
2026-10-18, delegated authority to redesign Phase 10 requirements where technically
justified, without weakening safety. Every change below was designed from DEVELOPMENT data
only (seeds 1, 611, 612, 613), calibrated there, and pinned before the fresh held-out seed
621 was built. No threshold was loosened: every number in `[monitoring]`, `[shadow]`,
`[probation]`, `[validation]` and the drill's `[checks]` is the 2026-10-11 value.

**1. Steady world (cause measured, dev seed 1).** The attempt-1 hypothesis "arrivals stop at
day 196, so the frozen model decays" was REFUTED: a world identical except for continued
arrivals decays the same way (active customers 694 -> 614 vs 693 -> 625 from day 196 to 238;
frozen day-195 champion 1.41 vs 1.43 x its backtest vWAPE by day 252, eu_west 2.43 vs 2.50).
The population already shrinks before day 196. The cause is the simulator's compounding,
heterogeneous per-customer growth (N(0.002, 0.004) per day, clipped to [-1%, +1.5%]): a few
fast-growing customers come to dominate each region (eu_west top-3 share 0.18 -> 0.28, top-10
0.44 -> 0.52, over days 112..251 with flat volume). That makes forecasting genuinely harder:
the seasonal naive's error rose 1.38x too. Test: the same world with growth switched off
(`growth_mean = 0`, `growth_sd = 0.0001`) keeps the frozen champion at 0.81-0.96x overall and
<= 1.20x on every slice for days 196..252 (no breach at all). Two alternatives that would
have kept the organic world were measured and REJECTED: monitoring skill (model vWAPE /
seasonal-naive vWAPE on the same window) stays <= 1.10 on the drift world too, because the
naive suffers equally on a ramp; and weighted bias separates the late steady rise (eu_west
bias -0.02 at 2.4x error) but the steady world's eu_central over-forecast (+0.09) is as large
as the drift world's bias at the deadline. So the steady world of the drill, which must be a
NULL control, is `configs/simulator/scenarios/mlops_steady.toml` = the pricing shadow world
with only organic growth switched off (`pricing_shadow.toml` itself is unchanged; Phases 6
and 9 still use it), and `mlops_drift.toml` is rebased on it (steady and drift differ only
by the injected changes, which are unchanged). Organic concentration is a real monitoring
limitation, documented in `docs/mlops.md`; it is not claimed solved.

**2. Persistence-aware alarms** (`[monitoring] persistence_days`). A decision signal over its
threshold on one day is a BREACH; it is an ALARM (retraining trigger; what the drill counts)
only when the same signal breached on `persistence_days` consecutive evaluated days of the
same champion. Consecutive 14-day windows share 13 days, so one extreme day lifts a single
window. Breaches stay in every report. Detection is counted from the drift onset.

**3. Continuous, safe promotion.** The drill operator approves every version the registry
reports READY (shadow passed against the CURRENT champion; `promote` re-checks it), in the
steady and the drift world, always as a named approver; probation applies to each promotion.
Attempt 1 approved only the first READY version, so champions aged. No automatic champion
change exists except a probation restore.

**4. Like-for-like recipe comparison** (`champion_non_inferiority`). The candidate's skill is
compared with the skill of the champion's RECIPE re-run on the candidate's identical rolling
origins and panel (fail closed if the folds differ). A candidate with the champion's recipe
is the identical backtest (deterministic fits; a test re-runs an equivalent recipe and gets
exactly 1.0), so it is not re-run: comparing the trained ARTIFACTS is shadow's job, on
identical live observations. Attempt 1 compared skills from different windows and rejected 4
of 8 drift retrains on 601 for having harder (drifted) weeks in their backtests.

**5. Faster drill, same results.** The three worlds run as spawned processes, one registry
and candidates directory each (LightGBM segfaults when trained from several threads of one
process, so threads were rejected); `OMP_WAIT_POLICY=PASSIVE` stops idle OpenMP threads
spinning. Per-world MLflow SQLite files avoid writer contention.

### Calibration (2026-10-24, development seeds 1, 611, 612, 613 only)
All four dev drills (with the provisional `persistence_days = 3`) passed 12 / 12 checks.
Evidence: `data/mlops/attempt2/calibration-seeds-1-611-612-613.txt` (local, gitignored) and
`docs/evidence/phase-10-attempt2-dev-seed*-drill.json`.

| Measurement | 1 | 611 | 612 | 613 |
| --- | --- | --- | --- | --- |
| Steady breach days (any decision signal) | 0 | 0 | 0 | 0 |
| Steady max residual ratio / max slice ratio | 0.97 / 1.20 | 0.98 / 1.21 | 1.01 / 1.39 | 1.00 / 1.48 |
| Drift first breach (onset 07-27, deadline 08-10) | 08-06 | 08-06 | 08-08 | 08-08 |
| Drift first alarm, k = 2 / 3 (drill) | 08-07 / 08-08 | 08-07 / 08-08 | 08-09 / **08-10** | 08-09 / **08-10** |
| Approved promotions steady / drift; probation restores | 2 / 2; 1 | 2 / 2; 1 | 2 / 2; 1 | 2 / 2; 0 |
| Good retrains passing `champion_non_inferiority` (excl. 3 bootstraps) | 11 / 11 | 11 / 11 | 10 / 10 | 11 / 11 |
| Bad recipe same-origin ratio (limit 1.02) | 1.057 | 1.154 | 1.095 | 1.110 |

**`persistence_days`.** Chosen by a rule written down before the 612 / 613 results were seen
(`data/mlops/attempt2/persistence-decision-rule.txt`): maximise min(k - longest steady breach
run, worst-seed days between first alarm and deadline), both >= 1; tie -> keep 3. With k = 3,
612 and 613 alarmed ON the deadline (0 days slack): infeasible. k = 2 gives margin 2 on the
steady side and 1 day of slack: chosen. Because no dev drill had run k = 2 end to end, two full
dev drills were re-run with k = 2 before pinning (seed 1 per the rule, plus 612, the tightest):
both PASS 12 / 12 (2026-10-25; first alarm 08-07 on seed 1 and 08-09 on 612, 0 steady alarm
days on both; reports `data/mlops/attempt2/k2/`, summary
`data/mlops/attempt2/calibration-k2-confirmation-seeds-1-612.txt`). No threshold was changed. The 1-day slack is thin: the drift detector
reacts 10-12 days after onset on dev worlds, so a held-out world whose ramp crosses the
threshold later than 08-09 would fail `drift_detected_within_days`; that is a property of the
unchanged 1.30 / 1.50 thresholds and the 14-day window, and it is reported, not tuned.

**`champion_non_inferiority`.** A good retrain against a champion of the same recipe is
exactly 1.0 by construction (42 of 43); the one exception is seed 613's rollback world, where
the incumbent was the force-promoted bad recipe and the good retrain scored 0.90 against it
(re-run on identical origins). The bad recipe as candidate scored 1.057-1.154, so the
unchanged 1.02 margin separates them on every dev seed. Retrain acceptance: every good
retrain passed validation on every dev world (attempt 1 on 601: 4 of 8 drift retrains
rejected).

### Held-out result (2026-10-25, seed 621, run once)
Pinned (hashes re-pinned 2026-10-25) and code frozen after `make check` exit 0; seed 621 was
built only then. Result: **12 / 12 PASS** (`docs/evidence/phase-10-attempt2-eval-seed621-drill.json`).
Steady world 0 breach / 0 alarm days; drift first alarm 08-09 (1 day before the deadline);
bad recipe rejected (`backtest_acceptance`, `champion_non_inferiority` 1.142); forced bad
champion restored by probation on 08-23 (1.082 > 1.05); 16 versions with complete lineage.
Three `make check` fixes made before the run (test fixture generalised for k = 2; coverage
measures spawned processes; dependency security upgrades) changed no threshold or model
code; they are listed in `docs/evidence/phase-10.md`.

## Defect found in Phase 4 serving (fixed here)
`RidgeBaseline.state()` is written with `sort_keys=True`; `from_state` rebuilt the
categorical one-hot blocks in JSON key order (dow, product, region, segment) instead of fit
order (region, product, segment, dow), so every LOADED demand artifact applied its
categorical coefficients to the wrong columns. Training, the Phase 4 backtest acceptance
(in-memory fold models) and the artifact checksums were unaffected; everything that LOADED
an artifact was: the forecast API and the Phase 6 / Phase 9 pricing input builder (and so
their shadow evaluations). On the development world (seed 1, 8-week pricing window) the
loaded artifact's vWAPE was 0.1623 vs 0.1558 with the fix (bias -0.3% in both); on the toy
fixture the error was larger (0.26 vs 0.16). The Phase 4 round-trip test compared state
dictionaries (order-blind). Fix: rebuild blocks in `CATEGORICAL` order and refuse unknown
categories; regression test compares predictions; `serving_parity` now guards every
candidate. The spent held-out pricing seeds were NOT re-run (that would turn them into a
tuning set). Owner decision 2026-10-25: disclose in the Phase 6 / 9 evidence, do not
re-evaluate now; a fresh pre-registered evaluation is required before live pricing.

## Consequences
* Retraining is automatic; promotion is approved; rollback is automatic only as a restore.
* The champion pointer, not an environment variable, decides what the API serves.
* Only the demand forecaster is registered. Recovery (Phase 8) and customer (Phase 9)
  artifacts and the pricing input builder still load fixed directories (carry-over).
* Served forecasts are re-scored point in time from the warehouse, not read from a log of
  what was served (Phase 11 should persist served forecasts).
* The churn archive (Phase 9) is not yet a registered model input (carry-over).
