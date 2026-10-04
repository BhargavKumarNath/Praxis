# Praxis: Autonomous Revenue Decision Infrastructure

A continuously operating pricing, payment-recovery, MLOps and AI-operations system for a
**simulated** API / compute business. All data is synthetic unless labelled otherwise.

Project contract: `CLAUDE.md`, `project.md`, `project_plan.md`, `required_test.md`.
Architecture: `docs/architecture.md`. Decisions: `docs/adr/`.

## Quick start

```bash
uv sync                # creates .venv from pyproject.toml / uv.lock
cp .env.example .env   # fill locally; never commit
make check             # lint, types, tests (local Postgres + Pub/Sub emulator via Docker),
                       # event chaos smoke, schemas, secret scan, frontend, terraform
make run               # http://127.0.0.1:8000/healthz
```

Status: Phase 3 complete (event backbone, see `docs/event-backbone.md`; evidence `docs/evidence/phase-3.md`). Progress: `project_progress.md`; evidence: `docs/evidence/`.
