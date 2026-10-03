.PHONY: data-dev dbt-build data-check setup lint typecheck test test-fast schemas sim-smoke secrets tf-check frontend-check check run

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
DBT = DBT_TARGET_PATH=data/dbt/target DBT_LOG_PATH=data/dbt/logs \
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

data-check:
	.venv/bin/pytest tests/data -q

run:
	.venv/bin/uvicorn --factory praxis.api.app:create_app --reload
