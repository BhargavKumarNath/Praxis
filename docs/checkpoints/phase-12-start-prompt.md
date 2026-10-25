Start Phase 12 (AI Revenue Operator). Goal: finish it properly in the minimum time.
Phases 10 and 11 are complete (uncommitted in the working tree); do not reopen them.

Read first, and only what you need: CLAUDE.md (especially s7 LLM / Agent Rules, s24 contracts),
the Phase 12 section of project_plan.md, section 18 of required_test.md (Phase 12 tests) and
section 23 (evidence template), the "Next" and "Carry-over" sections at the end of
project_progress.md (Phase 11 carry-over included), docs/observability.md, ADR 0004 (LLM does
not own pricing), ADR 0005, ADR 0013, ADR 0017, ADR 0018, docs/runbooks/agent-failure.md, and
the existing pricing / mlops / payments / ops CLIs and stores the agent tools will wrap.

Before coding, reply with a short plan (max ~25 lines): deliverables, the exact test gate, the
files you will create or change, the tool list with each tool's permission tier, any ADR
needed, and anything that needs my approval (cloud spend, new dependencies, any real LLM
call). Then proceed without waiting unless something needs my approval.

Known caveats carried into this phase (do not hide them; surface them in the evidence):
1. Phase 11 gate passed, but these are NOT wired: automatic feeds for data_source_age_s,
   data_contract_failures_total, business_kpi_deviation and queue/DLQ backlog sampling, and
   no scheduler runs OpsMonitor.tick. Agent investigation tools that read health or alerts
   must report "no data" honestly for metrics nothing feeds. Do not wire them unless a
   Phase 12 tool strictly needs it; if you do, keep it minimal and say so.
2. Telemetry is an in-process reader (no exporter): the agent sees only its own process's
   metrics. Say so in tool output and docs.
3. nightly-perf showed event-chaos throughput 0.69x of the local baseline (gate floor 0.5x),
   cause not isolated, baseline not re-recorded. If you touch hot paths, measure telemetry
   overhead (stubbed vs real) once and report it; do not re-record the baseline to hide it.
4. All business numbers are SYNTHETIC simulator output. The agent must label them so.
5. Phase 10 / 11 are uncommitted; the known gitleaks false positive
   (tests/payments/test_ids_and_model.py:44) is not a Phase 12 issue: report it, don't fix it.
6. Phase 4 loader defect: Phases 6 / 9 need a fresh pre-registered evaluation before live
   pricing. The agent must never present those shadow results as validated for execution.
7. Only one runbook (pubsub-backlog) is executed in a test. Agent runbook-following tools
   should not claim the others were exercised.

Work rules for speed (CLAUDE.md integrity and git rules still apply in full):
1. Run `make audit` first; fix new advisories immediately.
2. Local only. NO real LLM calls in tests or CI and no API keys in the repo. Put the model
   behind a small provider interface with a deterministic scripted fake for tests (the fake
   is a test double for the provider only; never mock the tools, validators, permission layer
   or audit under test). A real provider adapter, if built, must be opt-in, never run in
   `make check`, read its key from the environment, and need my approval before any live call
   or spend. The core system must keep working with the agent absent or the provider down.
3. Strict scope: exactly the project_plan Phase 12 deliverables and required_test s18 tests.
   No work from later phases (dashboard UI is Phase 13).
4. Agent boundaries (CLAUDE.md s7, ADR 0004): the agent may observe, retrieve evidence,
   investigate, explain, orchestrate allowed workflows, request low-risk actions and REQUEST
   guarded high-impact actions. It may not invent a price, call the optimiser or payment
   execution directly, bypass guardrails, run shell or arbitrary SQL, read secrets, silently
   deploy a model or silently change a live price. Every tool: typed schema, argument
   validation, permission enforced OUTSIDE the prompt, an audit event, and independently
   callable and testable. High-impact actions create a pending request that a named human
   approver completes through the existing approval paths.
5. Architecture: place `praxis.agent` in the import-linter layers in the same change, add the
   forbidden contract (agent must not import the optimiser or payment execution directly; it
   goes through the existing approval/request stores), add it to the "no web framework"
   contract, and add coverage floors (permission layer and tool validation >= 95). Respect the
   complexity budget (no new complexity-debt noqa). Any new table needs a migration (0007+).
6. Small vertical slices: tool schema -> permission check -> audit -> test, one tool family at
   a time (read-only evidence tools first, then low-risk actions, then guarded requests, then
   the investigation / explanation loop, then provider-outage fallback).
7. Fast inner loop: run only affected tests plus ruff and mypy on changed files while working.
   Run `make check` ONCE at the end (in the background while you write docs); it must exit 0.
   If it fails, fix the cause and rerun; never skip or loosen. If an agent evaluation needs
   scenarios, pilot on a tiny input first and pre-register acceptance thresholds before the run
   that judges them; never change them afterwards; any held-out scenario is evaluated once.
8. No run longer than ~15 minutes without telling me why. Use background jobs plus a single
   until-loop wait; do not poll.
9. Run `make nightly-perf` if you touch hot paths, `make nightly-security` if you touch
   dependencies or secrets handling (an LLM SDK would count: ask first).
10. Keep docs/checkpoints/phase-12-checkpoint.md updated at each milestone so a new session can
    resume quickly.
11. Never commit or push.

Finish with: docs/evidence/phase-12.md (s23 template, including the caveats above and any
limitation you found), an ADR for the agent design, docs (docs/agent.md, architecture.md,
observability.md if the agent emits telemetry, glossary if needed), CLAUDE.md s24 updates,
the agent-failure runbook updated from "forward-looking" to the real procedure and exercised
in a test, project_progress.md, and a concise report: what was built, test and gate results,
any amendments, open decisions. Then state plainly whether the gate passed, with the limits.
