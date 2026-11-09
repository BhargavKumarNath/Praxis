# Development

How to set Praxis up, run its checks, and reproduce the results in [results.md](results.md).

## Setup

You need Python 3.12, [uv](https://docs.astral.sh/uv/), Docker and Node 24. On macOS, install
the Xcode command line tools (`xcode-select --install`); PyMC compiles models with the system
C++ compiler.

```bash
git clone <repo> && cd praxis
make setup                 # uv sync + npm ci in frontend/
cp .env.example .env       # names only; fill in what you need, never commit .env
make pg-up pubsub-up       # Postgres on 127.0.0.1:55432, Pub/Sub emulator on 127.0.0.1:8085
make run                   # API on http://127.0.0.1:8000, probe /v1/ops/health
```

Nothing in `.env` is needed for the test suite. Keys are only used by the opt-in targets:
`PRAXIS_STRIPE_SECRET_KEY` (a sandbox `sk_test_` key) for `stripe-verify`,
`PRAXIS_FRED_API_KEY` / `PRAXIS_EIA_API_KEY` for live ingestion, and
`PRAXIS_ANTHROPIC_API_KEY` or `PRAXIS_GEMINI_API_KEY` for the live LLM benchmark.

## The gate

`make check` runs exactly what CI runs on every push, and must exit 0 before work counts as
done. It takes roughly 25-40 minutes because the tests run against real Postgres and the Pub/Sub
emulator.

| Target | What it checks |
| --- | --- |
| `lint`, `typecheck` | ruff (including a complexity budget) and strict mypy |
| `arch` | The eight import-linter layer and safety contracts |
| `ops-check` | SLO / alert configs, and that every alert has a complete runbook |
| `test`, `coverage-gate` | The pytest suite with per-file coverage floors (95 % for critical modules) |
| `events-check` | A chaos run of the event pipeline compared with an exact oracle |
| `schemas`, `secrets`, `audit` | Event schema sync, the secret scan, dependency audit |
| `frontend-check` | TypeScript, ESLint and the Vitest suite |
| `tf-check` | Terraform fmt, validate and a mocked apply / destroy test |
| `workflow-lint` | actionlint and zizmor over the GitHub workflows |

For a faster loop while editing, `make test-fast` skips the tests marked slow.

The nightly workflow adds three more: `nightly-science` rebuilds the development world and
re-applies every model's acceptance, `nightly-perf` compares throughput and memory with
`benchmarks/perf_baseline.json`, and `nightly-security` runs pip-audit, npm audit, gitleaks
over the full history and trivy.

## Tests

```text
tests/
  <package>/     one folder per package (simulator, streaming, pricing, agent, ...),
                 unit and Hypothesis property tests plus Postgres-backed integration tests
  unit/          foundation modules and repository hygiene
  contract/      event contracts and schemas
  integration/   the Pub/Sub emulator
  resilience/    twelve injected failures vs pre-registered criteria
  scale/         scale-ladder configuration and harness
  security/      route inventory and Terraform posture
  e2e/           seed data for the Playwright journey
frontend/src/test/   Vitest
frontend/e2e/        Playwright
```

Rules the suite follows: no test calls a live LLM or Stripe unless it is an explicit opt-in
target; tests use a real Postgres rather than mocking the code under test; thresholds that
judge results live in pinned configs (`configs/**`), and a hash test fails if one changes.

## Seeds

The model targets default to seed **42**, which is a held-out world. Always pass the
development seed when you experiment.

| Seed | Role | Run it? |
| --- | --- | --- |
| 1 | Development world for every phase | Yes |
| 611, 612, 613 | MLOps calibration worlds | Yes (development only) |
| 9001 | The live cloud world | Only through the live job |
| 42, 43 | Held-out: forecasting, elasticity, pricing Phase 6, recovery | No |
| 44, 45, 201-203, 301-303, 901-905 | Held-out: pricing | No |
| 601, 621 | Held-out: MLOps drill | No |
| 804 | Resilience fault seed, re-run by `make resilience` | Yes; new registrations start at 805 |

Re-running a held-out seed turns it into a tuning set, so its result can no longer be
reported as evidence.

## Reproducing results

| What | Command | Time |
| --- | --- | --- |
| Event pipeline, 1K customers | `make events-local` | about 1 min |
| Forecast | `make forecast-data forecast-backtest forecast-train FC_SEED=1` | about 8 min |
| Elasticity | `make elasticity-data elasticity-analyze elasticity-evaluate EL_SEED=1` | about 2 min plus PyMC compile |
| Pricing shadow | `make pricing-dev` | a few minutes |
| Recovery | `make recovery-data recovery-train recovery-evaluate RC_SEED=1` | about 6 min |
| MLOps drill | `make mlops-data mlops-drill ML_SEED=1` | about 100 min |
| Everything above, as CI does nightly | `make nightly-science` | 20-40 min |
| Agent benchmark (deterministic) | `make agent-bench` | under 1 min |
| Resilience suite | `make resilience` | 5-10 min |
| Browser journey | `make e2e-browsers e2e` | 1-3 min |
| Scale, 10K | `make scale-archive scale-run SCALE=10k-sharded` | about 2 min |
| Scale, 100K | `make scale-archive scale-run SCALE=100k` | about 40 min, needs about 11 GB RAM |

`make scale-m1 SCALE=m1-<n>` (the emulated 8 GB budget) uses systemd and cgroup v2, so it runs
on Linux only.

Opt-in targets that touch external services, never run by CI:

- `make stripe-verify`: Stripe Sandbox through the Stripe CLI in Docker. Representative only;
  never point anything at Stripe for load.
- `make bq-verify BQ_PROJECT=<project>`: the BigQuery sandbox.
- `make agent-llm-bench LLM_PROVIDER=anthropic`: the live LLM benchmark. It costs money and
  refuses to overwrite an existing report.

Some things do not reproduce bit for bit. Model versions include the code revision, so the
same data on a newer commit gets a new version id with the same metrics. Re-fetching the
public APIs returns new vintages (the raw archive replays the recorded bytes). Throughput
depends on the machine, which is why the performance gate uses wide bands. NumPy random
streams are only stable within a NumPy version; a golden checksum test fails on upgrade by
design.

## Conventions

- Every gate is a Makefile target, and the workflows only call `make <target>`.
- New packages are placed in a layer in `pyproject.toml` in the same change.
- New models and decision packages join the "models never import the simulator" contract.
- Coverage floors and the complexity-debt allowance may only go down.
- A decision that changes system behaviour gets an ADR in [adr/](adr/README.md).
- Changing a pre-registered threshold after seeing a result needs an ADR and owner approval,
  and the change is disclosed wherever the result is reported.
