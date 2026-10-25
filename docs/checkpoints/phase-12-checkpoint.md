# Phase 12 checkpoint (AI Revenue Operator)

Resume here. Gate and plan: `project_plan.md` Phase 12, `required_test.md` s18. ADR 0019.
Nothing is committed (owner commits). No live LLM call, no cloud spend, no new dependency.

## State (2026-10-25, late)

All slices are done (a to e), plus the benchmark (PASS 7/7, `docs/evidence/phase-12-benchmark.json`,
configs pinned before the scored run) and all the docs (ADR 0019, docs/agent.md, architecture,
observability, glossary, CLAUDE.md s24, runbook agent-failure, project_progress,
evidence phase-12.md).

`make check` exit 0 (1856 passed, 2 skipped; floors OK on 200 files). Gate PASS, written
into docs/evidence/phase-12.md. Phase 12 is DONE; nothing is committed. Next: the owner's
decisions (live LLM provider, gitleaks allowlist), then Phase 13.

## Decisions
- No live provider adapter (Gemini) built: open owner decision. Fallback + benchmark use the
  deterministic triage investigator (NOT an LLM) -> the benchmark measures the harness and
  the safety layer, not LLM competence. Must be said in the evidence.
- Freeze = guarded request -> human approval -> `pricing_freezes` row; execution refused by
  the store (memory) and by a Postgres trigger. Decisions are still recorded while frozen.
