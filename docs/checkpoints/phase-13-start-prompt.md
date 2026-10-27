Start Phase 13 (Product Dashboard and Decision Console). Goal: a dashboard that is correct
first, and that looks like a carefully designed product made by a senior design team, not
like AI output. Phases 0-12 and the Phase 11 wiring (ADR 0020) are done. Do not reopen them.

Read first, and only what you need:
- CLAUDE.md (s7 agent rules, s13 API rules, s17 security, s18 dependencies, s21 source
  freshness, s24 contracts).
- project_plan.md, the Phase 13 section (views and gate).
- required_test.md: s19 (Phase 13 tests) and s23 (evidence template).
- The "Next" and "Carry-over" sections at the end of project_progress.md.
- docs/observability.md, docs/agent.md, docs/pricing.md, docs/payments.md, docs/mlops.md.
- ADRs 0018, 0019 and 0020.
- docs/evidence/phase-11-wiring.md ("Readiness for Phase 13").
- The existing FastAPI app (src/praxis/api/) and the frontend placeholder (frontend/: Next.js
  16, React 19, TypeScript; nothing else installed yet).

Before coding, reply with a short plan (max ~30 lines):
- the read API endpoints per view, and which store or reader each one uses;
- the exact test gate, mapped to s19;
- the files you will create;
- every new dependency, with a justification under CLAUDE.md s18;
- the ADR(s) you will write;
- anything that needs my approval.
Then proceed without waiting unless something needs approval.

## Design: the part that matters most

Use your skills. Load `superpowers:brainstorming` (briefly), `frontend-design` and `dataviz`
before writing UI code. Load `run` (or Playwright) to see the real app. You may search the
web throughout.

1. Research before designing. Search for and study what the best operational and financial
   products do well: Stripe Dashboard, Linear, Vercel Observability, Datadog, Grafana,
   Bloomberg Terminal, FT and Economist data journalism (charts), Ramp, Mercury. Take
   principles, not visuals: density, hierarchy, how numbers, deltas, uncertainty and
   staleness are shown, and how empty and error states read. Never copy a brand, logo,
   palette or layout one to one.
2. Write a short design brief first, as `docs/design/dashboard.md`. It covers:
   - the point of view: a decision instrument for an operator, calm and precise, an
     instrument panel and not a marketing page;
   - the type system: a deliberate pairing, with tabular figures for every number;
   - the colour tokens: one accent; semantic colours for ok / degraded / stale / synthetic
     only; light and dark;
   - the spacing scale, grid, density, chart grammar (axes, gridlines, units, annotations,
     uncertainty bands), and motion (minimal, functional).
   Every later choice must trace back to this brief.
3. Explicitly avoid the "AI dashboard" look:
   - no purple or blue gradients, glassmorphism, glows, neon or blurred blobs;
   - no emoji, no decorative icons beside every heading, and no rows of identical rounded KPI
     cards each with an icon in a coloured circle;
   - no "Welcome back" hero, no generic Inter-on-white-with-shadows look, no shadcn defaults
     left untouched, no lorem ipsum or invented sample numbers;
   - no rounded-2xl everything, no chart junk, no pie or donut charts for time series, no
     rainbow categorical palettes.
4. Aim for the opposite:
   - an editorial, typographic layout: strong hierarchy, generous but purposeful whitespace,
     hairline rules instead of boxes where possible;
   - numbers set in tabular figures with units and comparison context (vs baseline,
     interval);
   - sparklines and small multiples where they carry meaning;
   - uncertainty shown as bands or intervals, never hidden;
   - status and staleness as quiet, precise text and indicators ("STALE 3h 12m", "SYNTHETIC",
     "shadow: not validated for execution"), not alarm colours everywhere.
   It must read like an instrument designed by one person with taste.
5. Iterate visually. After each view, run the app, take screenshots at 1440px and 390px, in
   light and dark, and critique them against the brief and the avoid-list. Fix the issues
   before moving on, and keep the final screenshots for the evidence. Mobile polish comes
   after correctness, but the layout must not break.

## Correctness (the gate; it wins over looks)

- Every number comes from a real API reading real state: Postgres stores, the DuckDB
  warehouse, and `praxis.ops.system.SystemReader` with its `inputs`. No hardcoded metrics, no
  mock data in the shipped app. Fixtures appear only in tests.
- Stale data is visibly marked per panel, using the snapshot and collector `inputs`. A
  missing input shows "no data, with the reason", never a 0.
- Synthetic data is labelled everywhere it appears. External sources are labelled with their
  provenance.
- Pricing results carry "shadow: not validated for execution" (Phase 4 loader defect).
- The AI operator view says the investigator is deterministic triage, not an LLM.
- Decision details render the recorded reasons, constraints, guardrails and lineage. Model
  versions are visible.
- A failed API call shows a safe error state with the correlation ID and no internals.
- The admin boundary: impactful controls (approve or reject agent requests, lift a freeze,
  promote or roll back) go only through the existing paths (`ApprovalDesk`,
  `ModelRegistry`, `FreezeStore`). They require server-side authorization (a token or role
  checked in FastAPI, never only in the UI) and a named approver. The UI can never set a
  price or bypass the optimiser.
- Views from the plan:
  - Executive;
  - Pricing;
  - Payments;
  - ML Operations;
  - System: Pub/Sub lag, API latency, errors, DLQ, source health, and a cost estimate that
    is clearly labelled as an estimate with its source, since no cost metric exists;
  - AI Operator: incidents, investigation trace from the audit, proposed actions,
    approvals, audit trail.
- s19 tests:
  - API contract tests (pytest, real Postgres);
  - frontend tests for the stale indicator, synthetic labels, decision details, model
    version, the error state, and the admin authorization path;
  - one Playwright end-to-end test of the key operational journey: a stale or alerting
    signal leads to an incident, its investigation trace, a pending request, and an
    authorized approval.
  Add the needed Makefile target(s) and a CI step, and update CLAUDE.md s24.

## Known caveats (surface them, do not hide them)

1. Telemetry costs about 32% of event-chaos throughput (measured, not fixed; Phase 14).
2. `queue_backlog` has no source on the Pub/Sub emulator. The dashboard shows "no data", not
   0.
3. All revenue, customers and recovery figures are SYNTHETIC simulator output.
4. Phase 4 loader defect: pricing shadow results are not validated for execution.
5. The agent benchmark measures the harness. No live LLM exists, and the Gemini adapter is an
   owner decision.
6. The gitleaks false positive (tests/payments/test_ids_and_model.py:44) is open. Report it,
   don't fix it.
7. Snapshots hold latest values only (no history). Charts that need history must read it
   from the stores of record (decisions, ledger, monitoring reports, warehouse) and say so.

## Work rules (CLAUDE.md integrity and git rules apply in full)

1. Run `make audit` first.
2. Local only, £0. No cloud resources and no live Stripe or LLM calls.
3. Check current official docs before relying on them (Next.js 16, React 19, any chart or
   test library, Playwright).
4. Dependencies: keep them few and justified. Pin exact versions and run `npm audit`.
   Because dependencies change, run `make nightly-security` before finishing and report the
   result.
5. Vertical slices, one view at a time: API endpoint, contract test, UI, frontend test,
   screenshot review. Start with System and AI Operator, then Pricing, ML Ops, Payments and
   Executive.
6. Keep the docs/checkpoints/phase-13-checkpoint.md checkpoint current. Tell me before any
   run longer than about 15 minutes.
7. Run `make check` once at the end, in the background. It must exit 0. If it fails, fix the
   cause and rerun; never skip a check or loosen a threshold.

## Finish with

- `docs/evidence/phase-13.md` in the s23 template, with the screenshots, caveats and
  limitations;
- an ADR for the dashboard architecture and the admin authorization boundary;
- `docs/dashboard.md` (how to run it, views, data sources, what each status means);
- `docs/design/dashboard.md` (the brief);
- updates to `architecture.md` and the glossary, and to CLAUDE.md s24;
- `project_progress.md`;
- a concise report: what was built, tests and gates, amendments, open decisions, and a
  plain statement of whether the gate passed, with its limits. Never commit or push.
