"""Operations: SLOs, alert rules, incidents, runbooks and health reports (Phase 11, ADR 0018).

Reads metric snapshots (``praxis.telemetry.MetricsView``); never imports model, pricing or
payment code, so an ops failure cannot affect decisions and models cannot see ops state.
"""
