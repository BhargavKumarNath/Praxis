# ADR 0006: Separate synthetic scale from real Stripe integration

Status: Accepted (Phase 0)

## Context
Creating many Stripe test customers would abuse the Sandbox and prove nothing about
Praxis's own throughput. Real integration risks (signatures, ordering, duplicates) are
distinct from scale risks (queue lag, DB pressure, cost).

## Decision
Two test tracks. Integration: a small Stripe Sandbox cohort exercising happy path,
declines, recovery, cancellation and Test Clocks. Scale: the deterministic simulator with
`SyntheticPaymentGateway` at 1K, 10K, 100K, then 1M customers, with measured results.
Neither track substitutes for the other, and no scale claim is made without a measurement.

## Consequences
* Reports must label results as simulated, Stripe Sandbox or measured infrastructure.
* Public free APIs are mocked or replayed in load tests.
