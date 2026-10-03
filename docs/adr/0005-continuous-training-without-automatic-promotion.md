# ADR 0005: Continuous training does not imply automatic promotion

Status: Accepted (Phase 0)

## Context
Drift is evidence for investigation, not proof that retraining helps. Auto-replacing a
production model on a drift signal can amplify a regression.

## Decision
Retraining may be automatic. Promotion is a separate, gated action:
drift detected, retraining requested, candidate trained, validated, challenger, shadow or
canary, promotion. Promotion requires passing validation (baseline comparison, segment
regressions, calibration, leakage, reproducibility) and keeps rollback to the previous
known-good version. Every artifact records model version, data version, feature version,
code revision, parameters, metrics, checksum and creation time.

## Consequences
* A safe auto-promotion policy would need its own ADR and tests.
* Registry lineage is mandatory, not optional.
