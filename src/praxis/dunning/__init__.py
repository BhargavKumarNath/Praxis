"""Operational dunning (Phase 8, ADR 0015): cases, decisions, scheduled retries (Cloud Tasks
abstraction), idempotent retry execution. Decisions come from ``praxis.recovery``; charges go
through ``PaymentGateway``. Never imports the simulator or ground truth.
"""
