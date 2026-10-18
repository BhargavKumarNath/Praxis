"""MLOps: model registry, continuous training, drift monitoring and safe promotion (Phase 10).

The Postgres control plane is the authoritative registry (ADR 0017); MLflow mirrors it.
Retraining may be automatic; promotion to champion is a separate, gated, approved action
(ADR 0005). See docs/mlops.md.
"""
