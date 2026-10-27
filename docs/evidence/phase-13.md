# Phase 13 evidence: Product Dashboard and Decision Console

```text
Phase: 13, Product Dashboard and Decision Console
Date: 2026-10-27
Code revision: bf72dda + uncommitted Phase 13 working tree (nothing committed or pushed)
Environment: local (Linux, Python 3.12, Postgres 17 in Docker `praxis-pg`, Node 24,
             Chromium headless shell 1248 for Playwright); no cloud resources, no Stripe or
             LLM calls, no API keys
```

## What was built

* `praxis.console`: a separate FastAPI process (top import layer, ADR 0021) with read routes
  for six views plus incident, session and decision details, and three guarded write routes
  (approve / reject a request, lift a freeze) through `ApprovalDesk`, `ModelRegistry` and
  `FreezeStore`, authorized by approver tokens checked in FastAPI.
* `praxis.api.middleware`: correlation ids, safe errors, request telemetry and the snapshot
  lifespan, extracted from `api.app` and shared by both apps.
* `frontend/`: the Next app (six views, decision and incident details, approver sign-in),
  hand-built SVG charts, design brief `docs/design/dashboard.md`.
* `make console-api`, `console-web`, `console-demo`, `e2e`, `e2e-browsers`; `frontend-check`
  now runs Vitest; CI job `e2e`.

## Tests executed

| Suite | Where | Result |
| --- | --- | --- |
| Console API contract (pytest, real Postgres + DuckDB) | `tests/console/` | 60 passed |
| Console coverage (branch) | `src/praxis/console/*`, floor 95% | 99% total; lowest files 96% (`executive.py`, `system.py`) |
| Frontend (Vitest + Testing Library, jsdom) | `frontend/src/test/` | 66 passed |
| End-to-end journey (Playwright, production build, fresh DB) | `frontend/e2e/journey.spec.ts` | 1 passed |
| Visual sweep: 6 views x {390, 1440} x {light, dark} | screenshots + scroll-width + console-error check | 0 horizontal overflow, 0 console errors |
| `make workflow-lint` (actionlint + zizmor) | `.github/workflows/` | clean |
| `make nightly-security` | gitleaks, pip-audit, npm audit, trivy | exit 2 on gitleaks (known false positive, below); pip-audit, npm audit (incl. dev) and trivy (run standalone) clean |
| `make check` | everything CI runs | exit 0 (below) |

required_test.md s19, item by item:

| Requirement | Test |
| --- | --- |
| API contract tests | `tests/console/test_views.py`, `test_admin.py`, `test_auth.py`, `test_warehouse_views.py`, `test_panels.py` |
| stale-data indicator | `trust.test.tsx` (view STALE after 2 refresh intervals and after a failed refresh, old numbers kept; source STALE with age); `test_views.py` (stale processes named and excluded) |
| no hardcoded production metrics | `hygiene.test.ts` (no app module imports a fixture; only `lib/api.ts` reaches the network); every number in the screenshots comes from the API |
| synthetic / external source labels | `trust.test.tsx` (Synthetic / Config / External), `test_views.py` (every panel has `source` and `synthetic`), source provenance in `test_warehouse_views.py` |
| decision details render correctly | `views.test.tsx` (reasons, guardrail pass / fail, constraints, lineage, checksum, no controls); `test_views.py` (decision detail returns the recorded audit) |
| model version visible | `views.test.tsx` (decision model versions; registry versions with stage, validation, data, features, code, checksum); `test_views.py` |
| failed API shows safe error state | `trust.test.tsx`, `api.test.ts` (fixed message + correlation id; oversized detail dropped; network error); `test_views.py` (store outage 503, unexpected failure 500 with only error + correlation id) |
| admin action requires correct authorization | `test_admin.py` (401 variants, 503 without approvers, body cannot name the approver, reject needs a reason, repeat approval 409, only three write routes, no price field in the schema), `test_auth.py`, `admin.test.tsx` |
| Playwright E2E, most important journey | `journey.spec.ts`: alert on System -> incident -> trace from the audit -> pending request (approve disabled) -> wrong token refused -> authorized approval named by the server -> active freeze -> second approval refused (409) |

Plan gate (project_plan.md Phase 13):

| Gate item | Evidence |
| --- | --- |
| dashboard reads real system state | Console reads Postgres, DuckDB and `SystemReader`; the demo was built through real CLIs (below) |
| no hardcoded metrics | hygiene test; fixtures only in `frontend/src/test/` |
| stale data visibly marked | view, process and source STALE states; lagging executive figure "N days behind" |
| synthetic data clearly labelled | SYNTHETIC on every view head, every simulator panel, the rail and decision records |
| role / admin boundary for impactful controls | ADR 0021; server-side token check; three write routes only |
| mobile polish secondary to correctness | 390 px screenshots, no page overflow; wide tables scroll inside their frame |

## Results

Screenshots (`docs/evidence/phase-13/`), from the demo database unless noted:
`executive-1440-light`, `executive-390-dark`, `pricing-1440-light`,
`pricing-detail-1440-dark`, `payments-1440-light`, `mlops-1440-light`, `system-1440-dark`,
`system-390-light`, `operator-1440-light`, `operator-detail-1440-light`, `operator-390-dark`,
and `mlops-drill-seed1-1440-light` (the console pointed at the existing seed-1 development
drill database `praxis_mlops_drill_drift_s1`, to review the monitoring charts against real
lifecycle output).

The demo database (`make console-demo`, development seed 1 only; no held-out seed touched):

1. the STAGED journey: the seed publishes a pricing snapshot with `price_change_fraction`
   0.8; the real `OpsMonitor` opens `price_volatility`; the real deterministic triage
   requests a freeze. The alert is staged, not produced by a pricing run;
2. `run-local` (300 customers, 28 days);
3. 42 dunning journeys on the SyntheticPaymentGateway through the real billing ->
   notification -> processor -> pipeline -> dunning consumer -> scheduled retry -> executor
   path (`tests/e2e/seed_dunning.py`); a fixed one in three customers fixes the card: 14
   recovered, 28 suspended;
4. the 8 seed-1 shadow pricing cycles: 40 decisions, 32 hold (no improvement), 8 frozen
   (churn response unidentified), 0 proposed price changes;
5. the seed-1 forecaster registered, validated (passed) and FORCE-promoted by the audited
   override (actor `console-demo`): the demo has no weeks of shadow days. The ML Ops view
   marks it Forced.

What the views show from that state, read off the screenshots:

* Pricing: 0 of 5 products would change price, matching the Phase 9 safe-pass caveat; every
  decision is labelled shadow, not validated for execution.
* Payments: recovered 14 of 42 cases (33%); the resolved-only rate reads 100% and is shown
  with its definition and "leaves out 28 open", so it cannot pass for the recovery rate.
* System: `queue_backlog` 0 here comes from the run-local in-memory broker's snapshot; on the
  Pub/Sub emulator it has no source and shows NO DATA (contract test). Dead letters 0 is the
  collector's measured 0. A stale console snapshot from an earlier process is named and left
  out. All four external sources read NO DATA: the seed-1 warehouse holds no external batches.
* Executive: the list price index lags the other figures by 57 days and is flagged.

## make check

Run once at the end, on the final tree. Exit 0 in 21 min. 1,948 passed and 2 skipped (the
opt-in live BigQuery and Stripe suites). Total coverage 98.82%, all coverage floors met (216
files, `src/praxis/console/*` at 95% included). All 7 import contracts kept, including "The
decision console cannot set prices, execute payments or reach ground truth". Lint, typecheck,
ops-check, events-check, schemas, secrets, pip-audit, npm audit, frontend-check (typecheck,
lint, Vitest 66 passed), tf-check and workflow-lint are clean. The Playwright journey is not
part of `make check`; it is its own target and CI job (`make e2e`), run and passed above.

While `make check` ran, three columns were added: the demand forecast and the elasticity by
segment (with its sd) on the Pricing latest cycle, and P(next retry succeeds) on the Payments
cases. These close plan items that were missing. The frontend stage of the same `make check`
ran on the edited files (Vitest 66, including the new test). Afterwards `make e2e` was run
again (1 passed) and the pricing and payments screenshots were retaken. The Python suites did
not need a re-run because no Python file changed.

After owner review (text read too thin), the sans face was changed from IBM Plex Sans to Inter
(variable, optical sizes), the type scale was moved up one step (12 / 14 / 16 px, nothing
smaller than 12), the `-webkit-font-smoothing: antialiased` override was removed, and the
forecast width was folded into the demand-forecast cell so the latest-cycle table fits at
1440 px. Screenshots are now 2x device pixels. Afterwards: `make frontend-check` (Vitest 66
passed), `make e2e` (1 passed), and the console-error sweep (0 on all six views) were re-run.
No Python file changed.

## Performance metrics

Not a performance phase. Local timings: `make console-demo` about 1 minute; the Playwright
journey 0.7 s inside a 10 s run (build cached); console pytest 8 s; Vitest under 1 s.

## Statistical metrics

None. The console computes no new statistic. Executive comparisons use the existing
28-day-median baseline from `praxis.console.executive`.

## Failures found during development (all fixed before the final runs)

1. Dead letters showed NO DATA when the collector had read an empty table (a measured 0).
   Fixed in `console.system.signals`; two contract tests.
2. Payments empty-state reason said `run-local` opens dunning cases; it does not. Corrected,
   and a contract test added.
3. Duplicate React keys on ML Ops lifecycle rows (one promotion writes two stage events);
   found by the console-error sweep on drill data. Fixed; regression test.
4. Weekly series drew no line (the daily gap rule broke every segment). Charts take a gap
   per series.
5. The journey seed failed on a populated database. Correct behaviour: the triage ranked the
   real queue backlog above the staged price signal by its pre-registered priority, and with
   real pricing decisions it needed a champion forecaster. The demo now runs the journey first
   on a clean control plane, as the E2E does, and registers the forecaster. Triage logic
   unchanged.
6. Lint (`react-hooks/set-state-in-effect`) on the data hooks: state now changes only in
   promise callbacks.
7. Test-expectation slips (stage label case, readout spacing, rounding) fixed in the tests,
   not the product.

No threshold, floor, tolerance or acceptance criterion was changed.

## Known limitations (carried caveats, surfaced, not hidden)

1. Telemetry costs about 32% of event-chaos throughput (Phase 11 wiring measurement).
   Unchanged here.
2. `queue_backlog` has no source on the Pub/Sub emulator: NO DATA, never 0.
3. All revenue, customer, payment and recovery figures are SYNTHETIC.
4. Pricing shadow results are not validated for execution (Phase 4 loader defect). Every
   pricing panel and decision says so.
5. The investigator is deterministic triage, not an LLM. The benchmark measures the harness;
   the Gemini adapter is an owner decision. The operator view says so.
6. gitleaks false positive `generic-api-key` at `tests/payments/test_ids_and_model.py:44`
   (commit 105c930) is still open and makes `make nightly-security` exit non-zero. Reported,
   not allowlisted, history not rewritten. No new finding.
7. Snapshots hold latest values only. The console draws history from the stores of record.

Also:

* The demo's alert is staged and its forecaster force-promoted (both audited and disclosed).
  Payments data comes from 42 synthetic-gateway journeys, not a Stripe run.
* Approver tokens are static configuration with no expiry, rotation or second factor.
* The executive view on the seed-1 warehouse has no external-source batches, so source
  health is NO DATA there.
* `npm run build` fetches Google Fonts at build time (`next/font`); an offline build would
  need local font files.

## Cloud cost incurred

GBP 0. No cloud resources, no Stripe or LLM calls.

## Gate

PASS, with the limits above.

## Reason

All six plan gate items and all eight required_test.md s19 items were executed and passed.
The console API contract tests ran on real Postgres and DuckDB, the component tests passed,
and the Playwright journey passed end to end on a fresh database. `make check` exited 0. The
pass covers the console as an instrument: real reads, visible stale and missing states,
synthetic, external and shadow labels, safe errors, and a server-side approver boundary with
no route that can set a price. It does not cover what the console displays: the pricing
results stay unvalidated for execution, the investigator is not an LLM, and every business
figure is simulated. `make nightly-security` still exits non-zero on the one known, open
gitleaks false positive. That owner decision is unchanged by this phase.
