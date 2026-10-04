"""Event backbone (Phase 3): transport, producer, archive, consumers and runtime.

Delivery is at-least-once and may be delayed, duplicated or reordered (ADR 0001, 0009).
Consumers are transport-agnostic: the same worker runs on the deterministic in-memory
broker (fault-injection tests) and on Pub/Sub (emulator or GCP).
"""
