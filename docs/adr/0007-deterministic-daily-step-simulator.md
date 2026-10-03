# ADR 0007: Deterministic daily-step simulator with retained ground truth

Status: Accepted (Phase 1)

## Context
Phases 4 to 9 need an event source with known latent parameters (elasticity, payment
reliability, churn sensitivity) so models can be validated against truth, and the whole
run must replay exactly. Per-request or per-hour customer simulation at 10K to 1M
customers is not affordable in Python at this stage.

## Decision
* **Time model:** customer behaviour steps daily; regional infrastructure is evaluated
  hourly (24 diurnal points per region per day) and aggregated for customers. Usage is an
  aggregate per customer, product and day; `request.completed` is a per customer-day
  aggregate (count, errors, p50/p95), not one event per request.
* **Determinism:** every random draw comes from `Generator(PCG64(SeedSequence([seed,
  stream, day])))`, one independent stream per purpose and day. Event, trace and
  correlation IDs are `blake2b(run_id, key)`, never counters, so they do not depend on
  iteration order. `published_at == occurred_at`; delivery faults arrive in Phase 3.
* **Reproducibility guard:** NumPy's `Generator` streams are not guaranteed across NumPy
  versions (NEP 19). The locked version is pinned by `uv.lock`, recorded in every
  manifest, and a golden checksum test fails loudly if it changes.
* **Ground truth:** latent parameters live only in `Population` arrays, persisted as
  `ground_truth.npz`. Events carry observables only (a test scans for leaked keys).
* **State machines:** customer and invoice lifecycles are explicit transition tables
  (`praxis.domain.states`). The `StreamValidator` replays any event stream against them.
* **Money:** integer pence (`*_minor`) and integer micro-GBP (`*_micros`); never floats.
* **Interventions:** controlled price experiments use hashed deterministic assignment
  (`blake2b(salt, customer_id)`), mutually exclusive per product, with exposure logged for
  both arms. Phase 5 builds the statistical pipeline on this.
* **Configuration:** `configs/simulator/default.toml`, validated by Pydantic, identified
  by `config_hash`. Changing it changes every run's identity.

## Consequences
* 10K customers x 28 days is about 1M events in roughly 17 s and 220 MB (measured).
* Sub-daily customer dynamics (intra-day price response) are out of scope.
* The simulator is a scientific instrument: do not tune it to flatter a model; difficult
  cohorts (price-insensitive, noisy-volume, unreliable payers) are deliberate.
