.PHONY: setup lint typecheck test secrets tf-check frontend-check check run

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

check: lint typecheck test secrets frontend-check tf-check

run:
	.venv/bin/uvicorn --factory praxis.api.app:create_app --reload
