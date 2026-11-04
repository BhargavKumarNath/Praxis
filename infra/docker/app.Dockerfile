# syntax=docker/dockerfile:1.7
# The Praxis Python image (ADR 0026): the API, the console API (sidecar), the jobs and the
# spend guard all run this one image with different commands. Built locally and pushed by
# digest (`make cloud-images`); Cloud Build is never used (no build minutes billed).
#
# Layout mirrors the repository (/app/src, /app/configs, /app/schemas, /app/docs/runbooks):
# the package resolves its pre-registered configs relative to the source tree.
# No model or warehouse is baked in: the live loop (ADR 0027) builds the SYNTHETIC warehouse
# and trains models on the shared live volume (PRAXIS_LIVE_ROOT). /app/dbt is the dbt project
# it runs. No secret, credential or .env is ever copied (.dockerignore allowlist).
FROM ghcr.io/astral-sh/uv:0.12.13@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 AS uv

FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --group pipeline --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --group pipeline

FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ARG REVISION=unknown
LABEL org.opencontainers.image.title="praxis-app" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.description="Praxis API / console API / jobs (synthetic data only)"
# libgomp1: LightGBM's OpenMP runtime (the forecast model's quantile heads).
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 praxis && useradd --uid 10001 --gid praxis --no-create-home --shell /usr/sbin/nologin praxis
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src
COPY pyproject.toml ./
COPY configs ./configs
COPY schemas ./schemas
COPY docs/runbooks ./docs/runbooks
COPY dbt ./dbt
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    MLFLOW_DISABLE_AGENT_HINT=1 DBT_SEND_ANONYMOUS_USAGE_STATS=false \
    PRAXIS_CODE_REVISION=${REVISION}
USER 10001:10001
EXPOSE 8080
HEALTHCHECK NONE
CMD ["uvicorn", "--factory", "praxis.api.app:create_app", "--host", "0.0.0.0", "--port", "8080"]
