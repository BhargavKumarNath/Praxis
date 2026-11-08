# Reproducibility guide

Every result in this case study can be rebuilt from the repository. Seeds, configs and tool
images are pinned, and model versions are content hashes. If you rebuild a development
world, you should get the same `data_version` and, for the deterministic parts, the same
decision ids. The exceptions are listed in section 5.

## 1. Environment

| Item | Pinned value |
| --- | --- |
| OS / host used for the evidence | Linux 7.0, 16 logical cores, 14.4 GiB RAM |
| Python | 3.12 (`requires-python >=3.12,<3.13`). Dependencies are locked in `uv.lock` (`uv sync`) |
| NumPy | Random streams are only stable within a NumPy version. A golden checksum test fails on upgrade, by design |
| Node | 24.x for `frontend/` (`npm ci`) |
| Docker images | `postgres:17-alpine`, the Pub/Sub emulator (`google-cloud-cli:emulators`), `hashicorp/terraform:1.16.5`, `aquasec/trivy:0.75.0`, `ghcr.io/gitleaks/gitleaks:v8.30.1`, `rhysd/actionlint:1.7.12`, `stripe/stripe-cli:v1.53.0`, `grafana/k6` pinned by digest (v1.3.0) |
| Secrets | `.env` (names in `.env.example`). Only needed for `stripe-verify` (sandbox `sk_test_`), live FRED / EIA ingestion and cloud targets |

```bash
uv sync
cd frontend && npm ci && cd ..
make pg-up pubsub-up        # local Postgres on 127.0.0.1:55432 and the Pub/Sub emulator on 127.0.0.1:8085
```

## 2. Seeds: which you may run

> **Warning.** The Makefile's model targets default to seed **42**, the held-out world. Always
> pass the development seed explicitly (`FC_SEED=1`, `EL_SEED=1`, `PX_SEED=1`, `RC_SEED=1`).
> Re-running a held-out seed turns it into a tuning set, and its result must not be reported
> as new evidence.

| Seed | Role | Run it? |
| --- | --- | --- |
| 1 | Development world (all phases), scale and resilience sim world | Yes |
| 611, 612, 613 | Phase 10 dev / calibration drill worlds | Yes (dev only) |
| 9001 | The live world (cloud) | Only through the live job. Never edit `state.json` |
| 42, 43 | Held-out: Phases 4, 5, 6 and the Phase 8 recovery world | **No**, results are recorded |
| 44, 45, 201-203, 301-303 | Held-out: Phase 9 pricing | **No** |
| 601, 621 | Held-out: Phase 10 drill | **No** |
| 701 / 801-804 | Fault seeds of Phases 14 / 15 (pre-registrations) | 804 is re-run by `make resilience`; new resilience seeds start at 805 |

## 3. Commands and expected durations (this host)

| What | Command | Duration | Notes |
| --- | --- | --- | --- |
| Everything CI runs | `make check` | about 30-40 min | Postgres plus the emulator. It must exit 0 |
| CI without the in-place Terraform init | `make lint typecheck arch ops-check test coverage-gate events-check schemas secrets audit frontend-check workflow-lint` | about 30 min | Used in Phase 16 |
| Terraform | `make tf-check` on a **clean copy** of `infra/terraform` (no `.terraform`, no `deploy.auto.tfvars`) | about 1-2 min | Mocked providers, GBP 0 |
| Science regression (dev world) | `make nightly-science` | about 20-40 min | Seed 1 only. A documented recovery dev known failure is allowed |
| Performance regression | `make nightly-perf` | about 2-5 min | Compared with `benchmarks/perf_baseline.json` (`local`) |
| Security | `make nightly-security` | about 2-5 min | pip-audit, npm audit, gitleaks (full history), trivy |
| Browser journey | `make e2e` (after `make e2e-browsers` once) | about 1-3 min | Fresh Postgres |
| Agent benchmark | `make agent-bench` | under 1 min | No LLM; writes `phase-12-benchmark.json` |
| Resilience | `make resilience` | about 5-10 min | Fault seed 804 criteria; writes the Phase 15 report |
| Stripe Sandbox | `make stripe-verify` | about 1-2 min | Needs `PRAXIS_STRIPE_SECRET_KEY=sk_test_...`. Representative only, **never a load test** |
| Forecast (dev) | `make forecast-data forecast-backtest forecast-train FC_SEED=1` | about 8 min | |
| Elasticity (dev) | `make elasticity-data elasticity-analyze elasticity-evaluate EL_SEED=1` | about 1.5 min plus PyMC compile | Needs g++ for PyTensor |
| Pricing shadow (dev) | `make pricing-data customer-train pricing-shadow PX_SEED=1` | a few minutes | Needs the seed-1 artifacts above |
| Recovery (dev) | `make recovery-data recovery-train recovery-evaluate RC_SEED=1` | about 6 min | |
| MLOps drill (dev) | `make mlops-data mlops-drill ML_SEED=1` (the default is 1) | about 100 min | 3 worlds in parallel |
| Scale 10K | `make scale-archive scale-run SCALE=10k-sharded` | about 2 min | |
| Scale 100K | `make scale-archive scale-run SCALE=100k` | about 40 min, about **11 GB RAM** | Run it in the background, and tell the owner first |
| Scale 1M | not runnable on a 14 GiB host | - | See [load-test-report.md](load-test-report.md) |

The durations are from the phase evidence files and the Phase 16 test report
([phase-16-test-report.json](../evidence/phase-16-test-report.json)).

## 4. Owner-only actions (never CI, never automatic)

- `make cloud-images | cloud-plan | cloud-apply | cloud-smoke CLOUD_PROJECT=praxis-dev-510522`.
  Always pass the project, because the default gcloud project is a different one. Apply only
  a saved and reviewed plan.
- `make cloud-destroy CONFIRM=destroy-praxis-dev-510522`: the only real teardown.
- `make bq-verify BQ_PROJECT=...`: the BigQuery sandbox.
- Approving or rejecting promotions, and lifting freezes, in the console.

## 5. Known sources of non-reproducibility

- **Code revision in version hashes.** Model versions include the code revision, so the same
  data trained on a newer commit gets a new version id while the metrics match. The cloud
  and local live trials agree on lifecycle events, not on version ids.
- **External APIs.** Re-fetching FRED, EIA, Open-Meteo or NESO returns new vintages. The raw
  archive replays the recorded bytes.
- **Machine load.** Throughput and latency vary with host load. The perf gate uses wide bands
  (0.5x / 1.5x), and the scale runs are single runs.
- **Wall-clock dependent tests.** Two Phase 7 tests once hard-coded a date. They now use
  wall clock + 1 day.
