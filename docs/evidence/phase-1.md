# Phase 1 Evidence Report

```text
Phase: 1 - Domain Model and Customer / Infrastructure Simulator
Date: 2026-10-03
Code revision: 2017cf0 (HEAD) + uncommitted working tree (no commits made, per policy)
Environment: Linux, 16 cores, 14 GB RAM, Python 3.12.14, numpy 2.5.3 (pinned in uv.lock)
All data below is SYNTHETIC. Nothing here is a real-world business result.
```

## Tests executed

`make check` (ruff, ruff format, mypy strict, pytest with coverage, payload-schema sync,
secret scan, frontend typecheck + lint, terraform fmt + validate) exits 0.
Pytest: **239 passed, 0 skipped, 0 xfail** (Phase 0 regression included).

| Required (required_test.md section 7) | Evidence |
| --- | --- |
| Determinism: same seed, config, state gives identical output | `test_determinism.py`: same-seed checksum equal; different seed and changed config differ; one engine replayed twice is equal; checksum independent of `PYTHONHASHSEED` (subprocess); golden checksum pinned to numpy 2.5.3. 10K run executed twice via CLI: identical stream checksum and byte-identical `events.ndjson` and `ground_truth.npz`. |
| Distribution: region, elasticity, reliability, tier mix, demand heterogeneity | `test_distributions.py`, 20,000 customers, tolerances pre-registered in the file header (4-sigma binomial; elasticity within 0.04 log units; reliability mean within 0.01). Difficult cohorts verified to exist. |
| Behaviour: price-sensitive cohorts reduce demand on controlled increase | `test_behaviour.py` price experiment (+30%, 50% hashed assignment). |
| Behaviour: lower reliability gives more failures | First-attempt failure vs ground-truth reliability. |
| Behaviour: known seasonality appears in aggregates | Weekly profile vs profile implied by ground-truth amplitudes. |
| Behaviour: capacity shocks affect intended variables only | Capacity shock, demand spike, product outage, structural unavailability tests. |
| No negative usage; no backwards entity timestamps | Schema bounds plus `StreamValidator` on every event. |
| State tests: all valid pass, all forbidden fail | `test_states.py`: exhaustive (state x trigger) enumeration for both machines, Hypothesis random walks, 26 impossible-stream rejection cases. |
| Scale smoke: 10K customers, record time, memory, events, checksum | See below; `test_scale_smoke.py` (slow) asserts the budget. |
| Event schemas valid (gate) | All 13 payload contracts; `StreamValidator` Pydantic-validates every event in 1K-scale runs and every 50th at 10K; exported JSON Schemas kept in sync by test. |
| Money integrity (CLAUDE.md section 12) | Invoice amount equals tier fee plus integer usage x price, asserted exactly; money fields are integer types. |

Coverage: 99% total (target 85%). Critical modules (branch coverage): `domain/states.py`
100%, `simulator/validation.py` 98%, `simulator/config.py` 100%, `simulator/engine.py` 99%.

## Performance metrics (10K customers, 28 days, seed 42, full stream validation)

Resource budget fixed before the first 10K run: wall clock <= 120 s, peak RSS <= 1536 MB.
Raw record: `docs/evidence/phase-1-scale-smoke-10k.json`.

| Metric | Value |
| --- | --- |
| Events | 977,754 |
| Wall clock | 17.0 s (two runs: 17.04 s, 17.19 s) |
| Peak RSS | 219 MB (`/usr/bin/time`: 224 MB) |
| Stream checksum (sha256) | `46c8bb7e1037fea042eb475a6f034ebfac502d0f840d7bc0b41f71daf8cf9675`, identical across both runs |
| Throughput | about 57K events/s including validation |
| Output size if persisted | 478 MB NDJSON (not committed; `data/` is gitignored) |

Event mix: usage 706,568; request.completed 187,833; price.exposed 33,457;
customer.created 10,000; subscription.started 9,385; payment.attempted 8,366;
invoice.created 7,603; payment.succeeded 7,430; conversion 1,441; subscription.changed
1,032; payment.failed 936; churn 343; service metrics 3,360. Reference 1K x 56 days run:
191,057 events in about 3.6 s.

## Statistical metrics (1,000 customers x 56 days unless stated, seed 42)

* Controlled price experiment, +30% on `api_requests`, 168 treated / 198 control analysed:
  difference-in-differences of log usage **-0.271** versus ground-truth prediction
  (`mean elasticity_treated x ln 1.3`) **-0.302**. Tolerance 0.12, passed. Price-sensitive
  segment (elasticity < -1.5): effect -0.556; insensitive (> -0.8): -0.114. Assignment is
  mutually exclusive and 50% (within 4 sigma).
* First-attempt payment failure: 1,540 attempts, observed 10.71% vs expected
  sum(1 - reliability)/n 9.31%, sigma 0.69 pp (2.0 sigma, tolerance 4 sigma). Least
  reliable half fails 18.2% vs 3.2% for the most reliable half (tolerance: at least 2x).
* Weekly seasonality: max absolute deviation of the normalised day-of-week profile from the
  ground-truth-implied profile 0.026 (tolerance 0.08); peak/trough ratio 1.153.
* Capacity shock (us_east x0.5): utilisation 0.526 to 0.956, p50 latency 43 to 118 ms,
  error rate 0.065% to 2.7%, throttled share 0% to 10.0%; other regions within 15%.
* Demand spike (eu_west x1.8): utilisation x1.68; other regions within 15%.
* Population at N=20,000: all share, elasticity (per tier and industry), heterogeneity and
  reliability tolerances passed; tier ordering of price sensitivity starter > growth >
  enterprise holds.

## Failures found

1. Ruff B023 (closure over loop variables) on the first payment-attempt implementation,
   a real design smell. Refactored into `Engine._attempt` with an explicit per-day context.
2. Two `conftest.py` files collided in mypy; helper import path was ambiguous.
3. Golden checksum could only be pinned after determinism was demonstrated (placeholder
   first, then verified value).
4. Coverage audit found critical modules below 95% (config validators 88%, runner 68%,
   validation 94%). No behaviour was wrong, but untested.

No scientific test failed on first execution. Two assertions pass with thin margin and are
recorded rather than adjusted: throttled share under the capacity shock is 10.01% against a
10% threshold, and the seasonality peak/trough ratio is 1.153 against 1.15. Both are fixed
by the seed, but any future change to the infrastructure or seasonality parameters should
be expected to disturb them and should be reviewed, not re-tuned silently.

## Fixes

1. Refactored the closure into a method.
2. `tests/` is now a package; helpers live in `tests/simulator/sim_helpers.py`.
3. Golden checksum pinned (`1d6b7dfb...a8015` for 200 customers x 14 days, seed 7).
4. Added `test_config_validation.py`, `test_runner.py` and 10 more validator cases.

No test or threshold was weakened after seeing a result. The simulator was not tuned to
make any result pass; parameters in `default.toml` are the first design values.

## Known limitations

* Daily customer time step; request events are daily aggregates (ADR 0007).
* No delivery faults (duplicates, delays, reordering): Phase 3.
* Elasticity recovery is checked here with a simple difference-in-differences against ground
  truth; the formal experiment pipeline, SRM and balance checks are Phase 5.
* The NumPy `Generator` stream is only stable per NumPy version. A NumPy upgrade will fail
  the golden test by design and requires a deliberate re-pin.
* The 10K run is single-process Python; 100K and 1M are not attempted (Phase 14).
* Churn is rare at this horizon (343 in 10K over 28 days), which limits churn-model data
  until longer horizons are simulated.
* `npm audit` dev-tool advisory from Phase 0 is unchanged.
* Nothing is committed; the Phase 0 limitation about `.gitignore` ignoring the contract
  files still stands.

## Cloud cost incurred

£0. No cloud resources used; all runs local.

## Gate

PASS

## Reason

Deterministic replay is proven at 200, 1K and 10K customers (identical checksums and
byte-identical output). Population heterogeneity is statistically verified against
pre-registered tolerances. The state machines and stream validator prove no impossible
transition occurs, and reject every deliberately corrupted stream. All 13 event types have
versioned payload contracts and validate. The 10K simulation ran in 17 s and 219 MB against
a 120 s / 1536 MB budget.
