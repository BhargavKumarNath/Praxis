# ADR 0017: Model registry, continuous training, drift monitoring and safe promotion

Status: Accepted (Phase 10). Written 2026-10-11. Pre-registered and hash-pinned
(`tests/mlops/test_config.py`): the lifecycle policy `configs/mlops/demand.toml`, the drift
world `configs/simulator/scenarios/mlops_drift.toml` and the drill acceptance
`configs/mlops/lifecycle_acceptance.toml`. The policy was amended three times BEFORE any
simulator world was built or monitored (see "Pre-run amendments") and once AFTER the
development world, with the owner's approval (see "Post-development amendment"); every
amendment is written into the file it changed.

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
tuning set); the owner decides whether Phases 6 / 9 need fresh pre-registered evaluations.

## Consequences
* Retraining is automatic; promotion is approved; rollback is automatic only as a restore.
* The champion pointer, not an environment variable, decides what the API serves.
* Only the demand forecaster is registered. Recovery (Phase 8) and customer (Phase 9)
  artifacts and the pricing input builder still load fixed directories (carry-over).
* Served forecasts are re-scored point in time from the warehouse, not read from a log of
  what was served (Phase 11 should persist served forecasts).
* The churn archive (Phase 9) is not yet a registered model input (carry-over).
