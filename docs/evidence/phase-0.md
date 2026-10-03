# Phase 0 Evidence Report

```text
Phase: 0 - Engineering Foundation and Architecture Contracts
Date: 2026-10-03
Code revision: 1beb499 (base commit) + uncommitted working tree (no commits made, per policy)
Environment: Linux, Python 3.12.14 (uv-managed .venv), Node 24.21, Terraform 1.16.5 (via Docker),
             Docker 29.8. No cloud resources used.
```

## Tests executed

| Check | Command | Result |
| --- | --- | --- |
| Ruff lint | `ruff check .` | pass |
| Ruff format | `ruff format --check .` | pass |
| Type check (mypy strict, owned code incl. tests) | `mypy` | pass, 17 files |
| Pytest suite (unit + contract + property) | `pytest --cov` | 69 passed, 0 skipped, 0 xfail |
| Backend import / startup | `uvicorn --factory praxis.api.app:create_app` + `GET /healthz` | HTTP 200, `X-Correlation-ID` echoed |
| Envelope schema round-trip | `tests/contract/test_event_envelope.py` | pass (incl. Hypothesis property) |
| Invalid envelope rejection | 25 mutation cases, JSON Schema and Pydantic must both reject | pass |
| Configuration validation | `tests/unit/test_config.py` | pass |
| `.env.example` names only, matches `Settings` | `tests/unit/test_repo_hygiene.py` | pass |
| Secret scan | `scripts/secret_scan.py`, `.env` not tracked | clean |
| Frontend type check | `npm run typecheck` | pass |
| Frontend lint / build | `npm run lint`, `next build` | pass |
| Terraform fmt | `terraform fmt -recursive -check` | pass |
| Terraform validate | `terraform -chdir=envs/dev validate` | pass |
| Python dependency audit | `pip-audit` | no known vulnerabilities |
| Frontend runtime dependency audit | `npm audit --omit=dev --audit-level=high` | 0 vulnerabilities |

## Performance metrics

Not applicable in Phase 0.

## Statistical metrics

Not applicable in Phase 0.

## Failures found

1. Pydantic model defaulted `schema_version`, so an envelope with no version was accepted
   while the JSON Schema rejected it. Contract disagreement.
2. Pydantic lax mode coerced `"yes"` to `True` for `is_synthetic`; the schema rejects it.
3. Pydantic's default Rust regex engine does not support the look-ahead used to reject an
   all-zero `trace_id`.
4. The secret scanner flagged fake credential-shaped fixtures in the repo's own tests.
5. The secret scan walked `frontend/node_modules` before `.gitignore` was updated.

## Fixes

1. `schema_version` is required on the model (`Literal[1]`, no default).
2. `is_synthetic` is `StrictBool`.
3. Envelope model uses `regex_engine="python-re"`.
4. Fixtures are assembled at runtime so the scanner stays strict instead of being relaxed.
5. `.gitignore` updated (secrets, venv, node_modules, Terraform state, data/model artifacts).

No test or threshold was weakened to obtain a pass; items 1 and 2 were genuine contract
defects found by the schema-vs-model agreement tests.

## Known limitations

* `npm audit` (including dev dependencies) reports 5 high-severity findings, all one chain:
  `braces` ReDoS via `eslint-config-next` -> `@next/eslint-plugin-next` -> `fast-glob` ->
  `micromatch`. This is lint-time tooling only and is not part of the shipped bundle. npm's
  suggested fix downgrades to Next 14, which was rejected. CI audits runtime dependencies
  (`--omit=dev`). Revisit in Phase 15 (dependency scanning) and when `eslint-config-next`
  ships a fixed chain.
* Terraform modules are validated but not planned or applied. `terraform plan` needs a
  project id and credentials, and nothing is provisioned without explicit approval.
* GitHub Actions workflow is written but has never run (no push, by policy).
* Pre-commit config is written; hooks are not installed into `.git/hooks` because that
  modifies repository state. Run `pre-commit install` when ready.
* `.gitignore` (user-authored) ignores `CLAUDE.md`, `project.md`, `project_plan.md`,
  `required_test.md` and itself, so the project contract is currently untracked.
* Postgres, Alembic migrations and a local Postgres service are not in Phase 0; no code
  connects to a database yet. `database_url` exists in config as an optional secret.
* Only the envelope contract exists. Per-event-type payload schemas arrive with their
  producers in Phase 1.

## Cloud cost incurred

£0. No cloud resources created. Terraform ran locally with `-backend=false`.

## Gate

PASS

## Reason

All Phase 0 gate items have evidence: the project boots locally, linting and strict type
checking pass, the pytest harness runs 69 tests at 99.5% branch coverage, Terraform fmt
and validate pass, the secret scan is clean and `.env.example` holds names only, and the
event envelope has a versioned JSON Schema with contract tests proving the schema and the
Pydantic model accept and reject identical documents. The known limitations above are
recorded rather than hidden.
