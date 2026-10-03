.PHONY: data-dev dbt-build data-check bq-verify setup lint typecheck test test-fast schemas sim-smoke secrets tf-check frontend-check check run

setup:
	uv sync
	cd frontend && npm ci

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

typecheck:
	.venv/bin/mypy

test:
	.venv/bin/pytest --cov

test-fast:
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

check: lint typecheck test schemas secrets frontend-check tf-check

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
