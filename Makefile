.PHONY: data-dev dbt-build data-check bq-verify setup lint typecheck arch test test-fast pytest-cov \
	coverage-gate schemas sim-smoke secrets audit tf-check frontend-check workflow-lint check run \
	pg-up pg-down pubsub-up pubsub-down pubsub-verify events-local events-bench events-check \
	forecast-data forecast-signals forecast-backtest forecast-train \
	elasticity-data elasticity-analyze elasticity-evaluate elasticity-contamination \
	pricing-data pricing-shadow pricing-dev nightly-science nightly-perf nightly-security perf-baseline \
	stripe-verify

# Every quality gate lives here; CI (.github/workflows/*.yml) only calls these targets, so
# `make check` locally means exactly what CI means. See CLAUDE.md "CI/CD and code health".

# --- Local dependencies (throwaway containers, loopback only, no cloud) -------------------
# Postgres uses trust auth on 127.0.0.1 only: no password exists to leak.
# CI_SERVICES=1 (set in CI): Postgres is a runner service container, so pg-up only waits.
CI_SERVICES ?= 0
PG_CONTAINER ?= praxis-pg
PG_PORT ?= 55432
PG_IMAGE ?= postgres:17-alpine
PRAXIS_TEST_DATABASE_URL ?= postgresql+psycopg://praxis@127.0.0.1:$(PG_PORT)/postgres
export PRAXIS_TEST_DATABASE_URL
PUBSUB_CONTAINER ?= praxis-pubsub
PUBSUB_PORT ?= 8085
PUBSUB_IMAGE ?= gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators
PUBSUB_EMULATOR = 127.0.0.1:$(PUBSUB_PORT)

# Pinned tool versions (bump deliberately; Dependabot does not see these).
ACTIONLINT_IMAGE ?= rhysd/actionlint:1.7.12
ZIZMOR_VERSION ?= 1.30.1
GITLEAKS_IMAGE ?= ghcr.io/gitleaks/gitleaks:v8.30.1
TRIVY_IMAGE ?= aquasec/trivy:0.75.0
STRIPE_CLI_IMAGE ?= stripe/stripe-cli:v1.53.0

pg-up:
ifneq ($(CI_SERVICES),1)
	@docker start $(PG_CONTAINER) >/dev/null 2>&1 || docker run -d --name $(PG_CONTAINER) \
		-p 127.0.0.1:$(PG_PORT):5432 -e POSTGRES_USER=praxis -e POSTGRES_HOST_AUTH_METHOD=trust \
		$(PG_IMAGE) >/dev/null
endif
	@.venv/bin/python scripts/wait_for.py --postgres $(PRAXIS_TEST_DATABASE_URL)

pg-down:
	-docker rm -f $(PG_CONTAINER)

pubsub-up:
	@docker start $(PUBSUB_CONTAINER) >/dev/null 2>&1 || docker run -d --name $(PUBSUB_CONTAINER) \
		-p 127.0.0.1:$(PUBSUB_PORT):8085 $(PUBSUB_IMAGE) \
		gcloud beta emulators pubsub start --host-port=0.0.0.0:8085 --project=praxis-local >/dev/null
	@.venv/bin/python scripts/wait_for.py --http http://$(PUBSUB_EMULATOR) --timeout 180

pubsub-down:
	-docker rm -f $(PUBSUB_CONTAINER)

setup:
	uv sync
	cd frontend && npm ci

# --- Fast static gates --------------------------------------------------------------------
lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

typecheck:
	.venv/bin/mypy

# Architecture contracts (layers, no ground truth in models, pure domain): pyproject.toml.
arch:
	.venv/bin/lint-imports

schemas:
	.venv/bin/python scripts/export_payload_schemas.py --check

secrets:
	.venv/bin/python scripts/secret_scan.py

# Known-vulnerability audit of the locked Python and production npm dependencies (network).
audit:
	@tmp=$$(mktemp) && uv export --locked --no-hashes --no-emit-project -o $$tmp >/dev/null && \
		.venv/bin/pip-audit -r $$tmp; rc=$$?; rm -f $$tmp; exit $$rc
	cd frontend && npm audit --omit=dev --audit-level=high

# GitHub workflow lint (actionlint) and security audit (zizmor).
workflow-lint:
	docker run --rm -v $(CURDIR):/repo -w /repo $(ACTIONLINT_IMAGE) -color
	uvx zizmor@$(ZIZMOR_VERSION) --offline .github/workflows

# --- Tests ----------------------------------------------------------------------------------
# Suite + coverage report (coverage.json feeds the per-module floors). Needs Postgres up.
pytest-cov:
	.venv/bin/pytest --cov --cov-report=term-missing:skip-covered --cov-report=json

# Per-module floors on top of the global fail_under (scripts/coverage_gate.py).
coverage-gate:
	.venv/bin/python scripts/coverage_gate.py coverage.json

# Full suite incl. emulator integration tests (local Docker; CI runs those in their own job).
test: pg-up pubsub-up
	PUBSUB_EMULATOR_HOST=$(PUBSUB_EMULATOR) $(MAKE) pytest-cov

test-fast: pg-up
	.venv/bin/pytest -m 'not slow' -q

sim-smoke:
	.venv/bin/python -m praxis.simulator --customers 10000 --days 28 --seed 42 --validate --schema-every 50

# Terraform via Docker when no local binary is installed.
TF = $(shell command -v terraform 2>/dev/null || echo docker run --rm -u $$(id -u):$$(id -g) -e HOME=/tmp -v $(CURDIR)/infra/terraform:/work -w /work hashicorp/terraform:latest)
tf-check:
	cd infra/terraform && $(TF) fmt -recursive -check
	cd infra/terraform && $(TF) -chdir=envs/dev init -backend=false -input=false >/dev/null
	cd infra/terraform && $(TF) -chdir=envs/dev validate

frontend-check:
	cd frontend && npm run typecheck && npm run lint

# Everything CI runs on a push / PR, in one command.
check: lint typecheck arch test coverage-gate events-check schemas secrets audit frontend-check \
	tf-check workflow-lint

# --- Phase 2 data platform (local DuckDB; no cloud resources) -----------------------------
SIM_DIR ?= data/sim
SIM_CUSTOMERS ?= 1000
SIM_DAYS ?= 56
SIM_START ?= 2026-01-05
SIM_END ?= 2026-03-01
DBT = DBT_TARGET_PATH=$(CURDIR)/data/dbt/target DBT_LOG_PATH=$(CURDIR)/data/dbt/logs \
	PRAXIS_DUCKDB_PATH=$${PRAXIS_DUCKDB_PATH:-data/warehouse/praxis.duckdb} \
	.venv/bin/dbt

dbt-build:
	$(DBT) build --project-dir dbt --profiles-dir dbt

# One command: simulate -> load raw -> ingest external signals -> dbt build -> freshness report.
# Sources that are down or lack an API key are reported and skipped; the build still completes.
data-dev:
	.venv/bin/python -m praxis.simulator --customers $(SIM_CUSTOMERS) --days $(SIM_DAYS) --seed 42 --validate --out $(SIM_DIR)
	.venv/bin/python -m praxis.data load-sim --dir $(SIM_DIR)
	.venv/bin/python -m praxis.data ingest --start $(SIM_START) --end $(SIM_END)
	$(MAKE) dbt-build
	-.venv/bin/python -m praxis.data freshness

# --- BigQuery verification (sandbox project, no billing; needs `gcloud auth application-default login`)
# A recent 42-day window: the sandbox expires partitions older than 60 days.
BQ_PROJECT ?=
BQ_END ?= $(shell date -u -d '-7 days' +%F)
BQ_START ?= $(shell date -u -d '-48 days' +%F)
BQ_DB = $(CURDIR)/data/warehouse/bq_source.duckdb
BQ_VARS = {start_date: '$(BQ_START)', end_date: '$(BQ_END)'}
BQ_DBT = DBT_LOG_PATH=$(CURDIR)/data/dbt/bq_logs .venv/bin/dbt

bq-verify:
	@test -n "$(BQ_PROJECT)" || (echo "usage: make bq-verify BQ_PROJECT=<sandbox project id>" && exit 1)
	rm -f $(BQ_DB)
	.venv/bin/python -m praxis.simulator --customers 1000 --days 42 --seed 42 --start-date $(BQ_START) --validate --out data/sim_bq >/dev/null
	.venv/bin/python -m praxis.data --db $(BQ_DB) --raw data/raw_bq load-sim --dir data/sim_bq
	.venv/bin/python -m praxis.data --db $(BQ_DB) --raw data/raw_bq ingest --start $(BQ_START) --end $(BQ_END)
	PRAXIS_DUCKDB_PATH=$(BQ_DB) DBT_TARGET_PATH=$(CURDIR)/data/dbt/bq_local $(BQ_DBT) build --project-dir dbt --profiles-dir dbt --vars "$(BQ_VARS)"
	.venv/bin/python -m praxis.data --db $(BQ_DB) bq-load --project $(BQ_PROJECT)
	PRAXIS_GCP_PROJECT_ID=$(BQ_PROJECT) PRAXIS_BQ_SCHEMA_PREFIX=praxis_dev_ DBT_TARGET_PATH=$(CURDIR)/data/dbt/bq_target \
		$(BQ_DBT) build --target bigquery --project-dir dbt --profiles-dir dbt --vars "$(BQ_VARS)"
	PRAXIS_BQ_LIVE_PROJECT=$(BQ_PROJECT) PRAXIS_BQ_PARITY_DB=$(BQ_DB) PRAXIS_BQ_DBT_RESULTS=$(CURDIR)/data/dbt/bq_target/run_results.json \
		.venv/bin/pytest tests/data/test_bigquery_live.py -m bigquery_live -v -s -p no:cacheprovider --no-cov

data-check:
	.venv/bin/pytest tests/data -q

run:
	.venv/bin/uvicorn --factory praxis.api.app:create_app --reload

# --- Phase 3 event backbone --------------------------------------------------------------
EVENTS_DB = postgresql+psycopg://praxis@127.0.0.1:$(PG_PORT)/praxis_events
EVENTS_CUSTOMERS ?= 1000
EVENTS_DAYS ?= 28

# Producer + consumers over the real Pub/Sub API (emulator) + Postgres. Free, local.
pubsub-verify: pg-up pubsub-up
	PUBSUB_EMULATOR_HOST=$(PUBSUB_EMULATOR) .venv/bin/pytest tests/integration -m pubsub_emulator \
		-v -p no:cacheprovider --no-cov

_events-db: pg-up
	@.venv/bin/python -c "from sqlalchemy import create_engine, text; \
	e = create_engine('$(PRAXIS_TEST_DATABASE_URL)', isolation_level='AUTOCOMMIT'); c = e.connect(); \
	c.execute(text('DROP DATABASE IF EXISTS praxis_events WITH (FORCE)')); \
	c.execute(text('CREATE DATABASE praxis_events'))"
	.venv/bin/python -m praxis.streaming --database-url $(EVENTS_DB) migrate

# Chaos run on the in-memory broker: duplicates, delay, reorder, crashes; verified vs oracle.
events-local: _events-db
	PRAXIS_LOG_LEVEL=ERROR .venv/bin/python -m praxis.streaming --database-url $(EVENTS_DB) run-local \
		--customers $(EVENTS_CUSTOMERS) --days $(EVENTS_DAYS) --archive data/events/archive \
		--report data/events/run_local.json

# Small chaos smoke used by `make check` / CI (exit code 1 if the oracle does not match).
events-check: _events-db
	@mkdir -p data/events
	PRAXIS_LOG_LEVEL=ERROR .venv/bin/python -m praxis.streaming --database-url $(EVENTS_DB) run-local \
		--customers 500 --days 28 --crash-rate 0.01 --duplicate-rate 0.2 \
		--report data/events/events-check.json >/dev/null

# Live latency over the emulator: paced producer, concurrent consumers.
events-bench: _events-db pubsub-up
	PUBSUB_EMULATOR_HOST=$(PUBSUB_EMULATOR) PRAXIS_LOG_LEVEL=ERROR .venv/bin/python -m praxis.streaming \
		--database-url $(EVENTS_DB) --environment bench-$$(date +%s) emulator-bench \
		--customers 50 --days 14 --rate 200 --publish-batch 20 --report data/events/emulator_bench.json

# --- Phase 4 demand forecasting (local DuckDB; no cloud) ----------------------------------
# Pre-registered evaluation world: scenario overlay + seed 42 (ADR 0010). Use FC_SEED=1
# FC_CUSTOMERS=300 for a development world while changing code; evaluate on seed 42.
FC_SCENARIO ?= configs/simulator/scenarios/forecast_eval.toml
FC_SEED ?= 42
FC_CUSTOMERS ?= 1000
FC_DIR ?= data/forecast/seed$(FC_SEED)-c$(FC_CUSTOMERS)
FC_DB = $(FC_DIR)/warehouse.duckdb
FC_DBT = DBT_TARGET_PATH=$(CURDIR)/$(FC_DIR)/dbt/target DBT_LOG_PATH=$(CURDIR)/$(FC_DIR)/dbt/logs \
	PRAXIS_DUCKDB_PATH=$(FC_DB) .venv/bin/dbt
FC_START = 2026-01-05
FC_END = 2026-07-19

# simulate -> raw load -> dbt marts for the forecast world (offline: no external fetch)
forecast-data:
	rm -f $(FC_DB)
	.venv/bin/python -m praxis.simulator --scenario $(FC_SCENARIO) --customers $(FC_CUSTOMERS) \
		--seed $(FC_SEED) --validate --out $(FC_DIR)/sim >/dev/null
	.venv/bin/python -m praxis.data --db $(FC_DB) --raw $(FC_DIR)/raw load-sim --dir $(FC_DIR)/sim
	$(FC_DBT) run --project-dir dbt --profiles-dir dbt --quiet

# Ablation input: real weather / carbon (and FRED / EIA with keys) for the world's dates.
# ~20 small live requests to free APIs; run once, then the raw archive replays offline.
forecast-signals:
	-.venv/bin/python -m praxis.data --db $(FC_DB) --raw $(FC_DIR)/raw ingest --start $(FC_START) --end $(FC_END)
	$(FC_DBT) run --project-dir dbt --profiles-dir dbt --quiet

forecast-backtest:
	.venv/bin/python -m praxis.forecasting --db $(FC_DB) backtest --scenario $(FC_SCENARIO) \
		--report $(FC_DIR)/backtest.json

forecast-train:
	.venv/bin/python -m praxis.forecasting --db $(FC_DB) train --backtest-report $(FC_DIR)/backtest.json \
		--out data/models/demand

# --- Phase 5 price elasticity (local DuckDB + PyMC; no cloud) ------------------------------
# Pre-registered evaluation world (ADR 0012): 8,000 customers, 28 pre-period days, five
# concurrent randomised price tests. Seed 42 = held-out evaluation (run once); EL_SEED=1 = dev.
EL_SCENARIO ?= configs/simulator/scenarios/elasticity_eval.toml
EL_SEED ?= 42
EL_CUSTOMERS ?= 8000
EL_NAME ?= eval
EL_DIR ?= data/elasticity/$(EL_NAME)-seed$(EL_SEED)-c$(EL_CUSTOMERS)
EL_DB = $(EL_DIR)/warehouse.duckdb
EL_DBT = DBT_TARGET_PATH=$(CURDIR)/$(EL_DIR)/dbt/target DBT_LOG_PATH=$(CURDIR)/$(EL_DIR)/dbt/logs \
	PRAXIS_DUCKDB_PATH=$(EL_DB) .venv/bin/dbt

# simulate -> raw load -> dbt marts (offline)
elasticity-data:
	rm -f $(EL_DB)
	.venv/bin/python -m praxis.simulator --scenario $(EL_SCENARIO) --customers $(EL_CUSTOMERS) \
		--seed $(EL_SEED) --validate --out $(EL_DIR)/sim >/dev/null
	.venv/bin/python -m praxis.data --db $(EL_DB) --raw $(EL_DIR)/raw load-sim --dir $(EL_DIR)/sim
	$(EL_DBT) run --project-dir dbt --profiles-dir dbt --quiet

# truth-free analysis: validity gates + estimators + hierarchical model (+ artifact if gates pass)
elasticity-analyze:
	PRAXIS_LOG_LEVEL=WARNING .venv/bin/python -m praxis.elasticity --db $(EL_DB) analyze \
		--out $(EL_DIR)/analysis --models data/models/elasticity

# pre-registered ground-truth acceptance (configs/elasticity/acceptance.toml)
elasticity-evaluate:
	.venv/bin/python -m praxis.science elasticity --analysis $(EL_DIR)/analysis \
		--sim $(EL_DIR)/sim --scenario $(EL_SCENARIO)

# Contamination robustness world: detection, ITT dilution, IV recovery. Validity is expected to
# flag the contaminated tests, so the analysis exits 1; the evaluation decides the outcome.
elasticity-contamination:
	$(MAKE) elasticity-data EL_SCENARIO=configs/simulator/scenarios/elasticity_contamination.toml EL_NAME=contamination
	-PRAXIS_LOG_LEVEL=WARNING .venv/bin/python -m praxis.elasticity \
		--db data/elasticity/contamination-seed$(EL_SEED)-c$(EL_CUSTOMERS)/warehouse.duckdb analyze \
		--out data/elasticity/contamination-seed$(EL_SEED)-c$(EL_CUSTOMERS)/analysis
	.venv/bin/python -m praxis.science elasticity \
		--analysis data/elasticity/contamination-seed$(EL_SEED)-c$(EL_CUSTOMERS)/analysis \
		--sim data/elasticity/contamination-seed$(EL_SEED)-c$(EL_CUSTOMERS)/sim \
		--scenario configs/simulator/scenarios/elasticity_contamination.toml

# --- Phase 6 pricing optimiser (local DuckDB + simulator truth; no cloud) -----------------
# Shadow world = the Phase 4 forecast world continued for 8 weekly cycles (pre-registered).
# PX_SEED=42 = held-out evaluation (run once); PX_SEED=1 = development. Needs the forecast
# artifact trained on that seed's forecast world (make forecast-data forecast-backtest
# forecast-train FC_SEED=<seed>) and the elasticity analysis of that seed (make elasticity-*).
PX_SCENARIO ?= configs/simulator/scenarios/pricing_shadow.toml
PX_SEED ?= 42
PX_CUSTOMERS ?= 1000
PX_DIR ?= data/pricing/seed$(PX_SEED)-c$(PX_CUSTOMERS)
PX_DB = $(PX_DIR)/warehouse.duckdb
PX_DBT = DBT_TARGET_PATH=$(CURDIR)/$(PX_DIR)/dbt/target DBT_LOG_PATH=$(CURDIR)/$(PX_DIR)/dbt/logs \
	PRAXIS_DUCKDB_PATH=$(PX_DB) .venv/bin/dbt
PX_ELASTICITY = data/elasticity/eval-seed$(PX_SEED)-c$(EL_CUSTOMERS)/analysis/report.json

# simulate -> raw load -> dbt marts for the pricing shadow world (offline)
pricing-data:
	rm -f $(PX_DB)
	.venv/bin/python -m praxis.simulator --scenario $(PX_SCENARIO) --customers $(PX_CUSTOMERS) \
		--seed $(PX_SEED) --validate --out $(PX_DIR)/sim >/dev/null
	.venv/bin/python -m praxis.data --db $(PX_DB) --raw $(PX_DIR)/raw load-sim --dir $(PX_DIR)/sim
	$(PX_DBT) run --project-dir dbt --profiles-dir dbt --quiet

# 8 shadow cycles + stress cycles, scored against simulator truth (shadow_acceptance.toml)
pricing-shadow:
	PRAXIS_LOG_LEVEL=WARNING .venv/bin/python -m praxis.science pricing-shadow --db $(PX_DB) \
		--sim $(PX_DIR)/sim --scenario $(PX_SCENARIO) --forecast-models data/models/demand \
		--elasticity-model data/models/elasticity --elasticity-report $(PX_ELASTICITY) \
		--out $(PX_DIR)/shadow

# Development world end to end (forecast artifact + elasticity evidence for seed 1 first).
pricing-dev:
	$(MAKE) forecast-data forecast-backtest forecast-train FC_SEED=1
	$(MAKE) elasticity-data elasticity-analyze EL_SEED=1
	$(MAKE) pricing-data pricing-shadow PX_SEED=1

# --- Nightly gates (`.github/workflows/nightly.yml`; also runnable locally) ---------------
# Too slow for every push. A failure here is a gate failure: investigate, never re-run until green.
PERF_DIR ?= data/perf
PERF_RUN = mkdir -p $(PERF_DIR) && \
	.venv/bin/python -m praxis.simulator --customers 10000 --days 28 --seed 42 > $(PERF_DIR)/sim.json && \
	PRAXIS_LOG_LEVEL=ERROR .venv/bin/python -m praxis.streaming --database-url $(EVENTS_DB) run-local \
		--customers 1000 --days 28 --report $(PERF_DIR)/events.json >/dev/null

# Science regression: rebuild the DEVELOPMENT world (seed 1; never the held-out seed 42) and
# apply every model's pre-registered acceptance. Each model phase appends its check here.
nightly-science:
	$(MAKE) forecast-data FC_SEED=1
	$(MAKE) forecast-backtest FC_SEED=1
	$(MAKE) elasticity-data EL_SEED=1
	$(MAKE) elasticity-analyze EL_SEED=1
	$(MAKE) elasticity-evaluate EL_SEED=1
	$(MAKE) elasticity-contamination EL_SEED=1
	$(MAKE) forecast-train FC_SEED=1
	$(MAKE) pricing-data PX_SEED=1
	$(MAKE) pricing-shadow PX_SEED=1

# Performance regression vs benchmarks/perf_baseline.json (per environment; scripts/perf_check.py).
nightly-perf: _events-db
	$(PERF_RUN)
	.venv/bin/python scripts/perf_check.py $(PERF_DIR)/sim.json $(PERF_DIR)/events.json

# Record a new baseline for THIS environment. Only with evidence (e.g. a measured speed-up).
perf-baseline: _events-db
	$(PERF_RUN)
	.venv/bin/python scripts/perf_check.py $(PERF_DIR)/sim.json $(PERF_DIR)/events.json --write

# Dependency audit + secrets in the FULL git history + Terraform misconfiguration (MEDIUM+;
# accepted findings live in .trivyignore with a reason and an expiry).
nightly-security: audit
	docker run --rm -v $(CURDIR):/repo $(GITLEAKS_IMAGE) git /repo --redact --no-banner
	docker run --rm -v $(CURDIR):/repo -w /repo $(TRIVY_IMAGE) config infra/terraform \
		--severity MEDIUM,HIGH,CRITICAL --ignorefile .trivyignore --exit-code 1 --quiet

# --- Phase 7 Stripe Sandbox (opt-in, live; never in CI) ------------------------------------
# Needs a SANDBOX key (sk_test_...) as PRAXIS_STRIPE_SECRET_KEY in .env and Docker for the
# Stripe CLI (`stripe listen` forwards real signed webhooks to a local uvicorn server).
# Four customers on Test Clocks (deleted afterwards); never a load test (CLAUDE.md s5).
stripe-verify: pg-up
	PRAXIS_STRIPE_LIVE=1 STRIPE_CLI_IMAGE=$(STRIPE_CLI_IMAGE) .venv/bin/pytest tests/payments/test_stripe_sandbox.py \
		-m stripe_live -v -p no:cacheprovider --no-cov
