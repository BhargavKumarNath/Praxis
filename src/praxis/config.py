"""Environment configuration model.

All configuration is read from environment variables (prefix ``PRAXIS_``) or a local
``.env`` file. Secrets are ``SecretStr`` so they never appear in ``repr`` or logs.
Nothing here touches the network at import time.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
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

    # Stripe: names only in Phase 0; the gateway lands in Phase 7.
    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None

    # External data API keys (Phase 2). Open-Meteo and NESO Carbon Intensity need none.
    fred_api_key: SecretStr | None = None
    eia_api_key: SecretStr | None = None

    @model_validator(mode="after")
    def _require_cloud_identity_outside_local(self) -> Self:
        if self.environment is not Environment.LOCAL and not self.gcp_project_id:
            msg = f"PRAXIS_GCP_PROJECT_ID is required when environment={self.environment.value}"
            raise ValueError(msg)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
