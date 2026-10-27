# Phase 13 checkpoint (Product Dashboard and Decision Console)

Updated: 2026-10-27. Nothing committed (owner commits).

## Done
- `make audit` at start: clean (pip-audit 0, npm audit 0).
- Design brief `docs/design/dashboard.md` (palette validated with the dataviz validator).
- `praxis.api.middleware` (correlation ids, safe errors, request telemetry, snapshot lifespan)
  extracted from `api.app`; both apps use it.
- `praxis.console` (new top layer, forbidden contract "cannot set prices, execute payments or
  reach ground truth"; coverage floor 95): panels envelope, auth (token-named approvers),
  readers for system / operator / pricing / payments / mlops / executive, cost estimate
  (`configs/console/cost.toml`), app with 3 guarded admin routes + whoami.
- `Settings.console_approvers` / `.env.example`.
- `tests/console/*`: 57 tests on real Postgres + DuckDB, console modules 96-100% covered.
  `tests/console/journey.py` drives the real alert -> incident -> triage -> pending request path.
- Frontend dev deps pinned: vitest 5.0.3, jsdom 30.1.2, @testing-library/react 16.3.3,
  @testing-library/dom 10.4.2, @playwright/test 1.64.0 (all >= 2 weeks old); npm audit 0.

- Frontend shell, tokens, trust-state components, SVG charts; System and AI Operator views
  (screenshot-reviewed); vitest suite (45 tests: trust states, error state, admin path, hygiene).
- System fix found by screenshot review: an empty dead_letters table read by a working
  collector is a measured 0 (was shown as no data); contract tests added.
- `make console-demo` (throwaway DB, dev seed 1 only; staged journey; forecaster
  FORCE-promoted, audited, actor console-demo), `console-api`, `console-web`, `e2e` targets.

- All six views built and screenshot-reviewed (1440 / 390, light / dark): fixes found by
  review: DLQ measured 0, weekly line gaps, duplicate React keys on ML Ops events, a wrong
  payments empty-state reason (run-local opens no dunning case), misleading 100% resolved
  recovery rate (now led by recovered of all cases), lagging price index flagged.
- Vitest 65 tests; console pytest 60; Playwright journey PASS (`make e2e`, CI job `e2e`).
- `tests/e2e/seed_dunning.py` (real synthetic-gateway dunning path) in `make console-demo`.
- ADR 0021, docs/dashboard.md, architecture, glossary, CLAUDE.md s24.

- `make nightly-security`: only the known gitleaks false positive. `make check` exit 0
  (1,948 passed, 2 skipped). Evidence written; gate PASS.

## Next
Phase 13 complete. Owner review; nothing committed.
