.PHONY: data-dev dbt-build data-check bq-verify setup lint typecheck test test-fast schemas sim-smoke secrets tf-check frontend-check check run \
	pg-up pg-down pubsub-up pubsub-down pubsub-verify events-local events-bench events-check \
	forecast-data forecast-signals forecast-backtest forecast-train

# --- Phase 3 local dependencies (throwaway containers, loopback only, no cloud) ------------
# Postgres uses trust auth on 127.0.0.1 only: no password exists to leak.
PG_CONTAINER ?= praxis-pg
PG_PORT ?= 55432
PG_IMAGE ?= postgres:17-alpine
PRAXIS_TEST_DATABASE_URL ?= postgresql+psycopg://praxis@127.0.0.1:$(PG_PORT)/postgres
export PRAXIS_TEST_DATABASE_URL
PUBSUB_CONTAINER ?= praxis-pubsub
PUBSUB_PORT ?= 8085
PUBSUB_IMAGE ?= gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators
PUBSUB_EMULATOR = 127.0.0.1:$(PUBSUB_PORT)

pg-up:
	@docker start $(PG_CONTAINER) >/dev/null 2>&1 || docker run -d --name $(PG_CONTAINER) \
		-p 127.0.0.1:$(PG_PORT):5432 -e POSTGRES_USER=praxis -e POSTGRES_HOST_AUTH_METHOD=trust \
		$(PG_IMAGE) >/dev/null
	@.venv/bin/python scripts/wait_for.py --postgres $(PRAXIS_TEST_DATABASE_URL)

pg-down:
	-docker rm -f $(PG_CONTAINER)

pubsub-up:
	@docker start $(PUBSUB_CONTAINER) >/dev/null 2>&1 || docker run -d --name $(PUBSUB_CONTAINER) \
		-p 127.0.0.1:$(PUBSUB_PORT):8085 $(PUBSUB_IMAGE) \
		gcloud beta emulators pubsub start --host-port=0.0.0.0:8085 --project=praxis-local >/dev/null
	@.venv/bin/python scripts/wait_for.py --http http://$(PUBSUB_EMULATOR) --timeout 120

pubsub-down:
	-docker rm -f $(PUBSUB_CONTAINER)

setup:
	uv sync
	cd frontend && npm ci

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

typecheck:
	.venv/bin/mypy

# Full suite incl. emulator integration tests (local Docker; CI runs them in a separate job).
test: pg-up pubsub-up
	PUBSUB_EMULATOR_HOST=$(PUBSUB_EMULATOR) .venv/bin/pytest --cov

test-fast: pg-up
	.venv/bin/pytest -m 'not slow' -q

schemas:
	.venv/bin/python scripts/export_payload_schemas.py --check

sim-smoke:
	.venv/bin/python -m praxis.simulator --customers 10000 --days 28 --seed 42 --validate --schema-every 50

secrets:
	.venv/bin/python scripts/secret_scan.py

# Terraform via Docker when no local binary is installed.
TF = $(shell command -v terraform 2>/dev/null || echo docker run --rm -u $$(id -u):$$(id -g) -e HOME=/tmp -v $(CURDIR)/infra/terraform:/work -w /work hashicorp/terraform:latest)
tf-check:
	cd infra/terraform && $(TF) fmt -recursive -check
	cd infra/terraform && $(TF) -chdir=envs/dev init -backend=false -input=false >/dev/null
	cd infra/terraform && $(TF) -chdir=envs/dev validate

frontend-check:
	cd frontend && npm run typecheck && npm run lint

check: lint typecheck test events-check schemas secrets frontend-check tf-check

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
	PRAXIS_LOG_LEVEL=ERROR .venv/bin/python -m praxis.streaming --database-url $(EVENTS_DB) run-local \
		--customers 300 --days 28 --crash-rate 0.01 --duplicate-rate 0.2 >/dev/null

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
