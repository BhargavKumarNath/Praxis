"""Environment configuration model.

All configuration is read from environment variables (prefix ``PRAXIS_``) or a local
``.env`` file. Secrets are ``SecretStr`` so they never appear in ``repr`` or logs.
Nothing here touches the network at import time.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    PROD = "prod"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PRAXIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = Environment.LOCAL
    log_level: LogLevel = LogLevel.INFO
    service_name: str = Field(default="praxis-api", min_length=1)

    # Control plane (Postgres). Optional in Phase 0: nothing connects yet.
    database_url: SecretStr | None = None

    # GCP. Prefix is fixed by the project contract.
    gcp_project_id: str | None = None
    gcp_region: str = "europe-west2"
    gcp_resource_prefix: str = "praxis"

    # Cost guard: caps applied to every deployable service.
    cloud_run_max_instances: int = Field(default=2, ge=1, le=10)

    # Stripe (Phase 7): sandbox only (the client refuses live keys). The API version is
    # pinned so responses never change shape with the account default (ADR 0014).
    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None
    stripe_api_version: str = Field(default="2026-09-30.endive", min_length=1)
    stripe_api_base_url: str = "https://api.stripe.com"
    stripe_timeout_s: float = Field(default=20.0, gt=0, le=60)
    stripe_webhook_tolerance_s: int = Field(default=300, ge=1, le=900)

    # External data API keys (Phase 2). Open-Meteo and NESO Carbon Intensity need none.
    fred_api_key: SecretStr | None = None
    eia_api_key: SecretStr | None = None

    # Dunning (Phase 8, ADR 0015). The retry-task endpoint answers 503 until configured.
    # tasks_token: shared secret Cloud Tasks sends in X-Praxis-Task-Token (on top of Cloud
    # Run IAM / OIDC). Unset recovery_model_dir = the deterministic baseline policy.
    tasks_token: SecretStr | None = None
    tasks_target_url: str | None = None
    tasks_service_account: str | None = None
    recovery_model_dir: Path | None = None

    # Demand forecasting (Phase 4). Unset model dir = forecast endpoints answer 503.
    forecast_model_dir: Path | None = None
    warehouse_path: Path = Path("data/warehouse/praxis.duckdb")

    @model_validator(mode="after")
    def _require_cloud_identity_outside_local(self) -> Self:
        if self.environment is not Environment.LOCAL and not self.gcp_project_id:
            msg = f"PRAXIS_GCP_PROJECT_ID is required when environment={self.environment.value}"
            raise ValueError(msg)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
