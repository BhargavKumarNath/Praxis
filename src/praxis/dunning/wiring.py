"""Composition of the cloud dunning stack from settings (Stripe Sandbox + Cloud Tasks).

Returns ``None`` (the endpoint answers 503) unless every required setting is present, so a
partially configured deployment never runs half a dunning pipeline. The recovery model is
optional: without a usable artifact the deterministic baseline decides (recorded as the
fallback reason on every decision).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from praxis.config import Settings
from praxis.control.db import make_engine
from praxis.dunning.executor import Charger, RetryExecutor
from praxis.dunning.service import DunningService
from praxis.dunning.tasks import CloudTasksQueue, CloudTasksTarget, google_token_provider
from praxis.payments.store import PostgresRefStore
from praxis.payments.stripe_client import StripeClient
from praxis.payments.stripe_gateway import StripeGateway
from praxis.recovery.artifact import ArtifactError, RecoveryArtifact, load_artifact
from praxis.recovery.config import load_policy
from praxis.recovery.policy import RecoveryDecider

logger = logging.getLogger(__name__)
TokenFactory = Callable[[], Callable[[], str]]


def load_recovery_artifact(path: Path | None) -> RecoveryArtifact | None:
    """The artifact the operator named explicitly, or None (baseline).

    A directory of artifacts is refused rather than resolved to the newest one: dropping a new
    artifact next to the old must never promote it (retraining is not promotion, ADR 0005).
    """
    if path is None:
        return None
    if not (path / "manifest.json").is_file():
        logger.error("recovery.artifact_not_selected", extra={"path": str(path)})
        return None
    try:
        return load_artifact(path)
    except ArtifactError as exc:
        logger.error("recovery.artifact_rejected", extra={"error": str(exc)})
        return None


def build_cloud_executor(
    settings: Settings, *, token_factory: TokenFactory = google_token_provider
) -> RetryExecutor | None:
    required = (
        settings.database_url,
        settings.stripe_secret_key,
        settings.tasks_token,
        settings.gcp_project_id,
        settings.tasks_target_url,
        settings.tasks_service_account,
    )
    if any(v is None for v in required):
        return None
    assert settings.database_url and settings.stripe_secret_key and settings.tasks_token  # noqa: S101 - narrowed above
    engine = make_engine(settings.database_url.get_secret_value())
    gateway = StripeGateway(
        StripeClient(
            settings.stripe_secret_key,
            api_version=settings.stripe_api_version,
            base_url=settings.stripe_api_base_url,
            timeout_s=settings.stripe_timeout_s,
        ),
        PostgresRefStore(engine),
    )
    policy = load_policy()
    decider = RecoveryDecider(policy, load_recovery_artifact(settings.recovery_model_dir))
    target = CloudTasksTarget(
        project=str(settings.gcp_project_id),
        location=settings.gcp_region,
        queue=f"{settings.gcp_resource_prefix}-{settings.environment.value}-payment-retries",
        url=str(settings.tasks_target_url),
        service_account_email=str(settings.tasks_service_account),
        task_token=settings.tasks_token.get_secret_value(),
    )
    queue = CloudTasksQueue(target, token_factory())
    service = DunningService(engine, decider, queue)
    return RetryExecutor(engine, {gateway.provider: Charger.for_gateway(gateway)}, policy, service)
