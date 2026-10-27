"""FastAPI application: health, demand forecasts, Stripe webhooks, ops endpoints.

Correlation ids, request telemetry and safe error bodies come from ``praxis.api.middleware``
(shared with the decision console)."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from pydantic import BaseModel

from praxis import __version__
from praxis.api.forecast import ForecastProvider, build_forecast_service, forecast_router
from praxis.api.middleware import ErrorResponse, install_middleware, publisher_lifespan
from praxis.api.ops import ops_router
from praxis.api.tasks import RetryHandler, tasks_router
from praxis.api.webhooks import webhook_router
from praxis.config import Settings, get_settings
from praxis.control.db import make_engine
from praxis.control.metric_snapshots import MetricSnapshotStore, SnapshotPublisher
from praxis.logging import configure_logging
from praxis.ops.config import load_ops_config
from praxis.ops.incidents import IncidentStore, PostgresIncidentStore
from praxis.ops.system import SystemReader, build_reader
from praxis.payments.store import PostgresInbox
from praxis.payments.webhook import WebhookReceiver

logger = logging.getLogger(__name__)
__all__ = ["ErrorResponse", "HealthResponse", "create_app"]


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str


def build_webhook_receiver(settings: Settings) -> WebhookReceiver | None:
    """Webhooks need a signing secret and the control-plane database; else they answer 503."""
    if settings.stripe_webhook_secret is None or settings.database_url is None:
        return None
    inbox = PostgresInbox(make_engine(settings.database_url.get_secret_value()))
    return WebhookReceiver(
        inbox,
        [settings.stripe_webhook_secret.get_secret_value()],
        tolerance_s=settings.stripe_webhook_tolerance_s,
    )


def build_retry_handler(settings: Settings) -> RetryHandler | None:
    """Production wiring (Stripe + Cloud Tasks) only when fully configured; else 503."""
    from praxis.dunning.wiring import build_cloud_executor

    executor = build_cloud_executor(settings)
    return executor.execute if executor is not None else None


def build_incident_store(settings: Settings) -> IncidentStore | None:
    if settings.database_url is None:
        return None
    return PostgresIncidentStore(make_engine(settings.database_url.get_secret_value()))


def build_system_view(settings: Settings) -> tuple[SystemReader, SnapshotPublisher] | None:
    """Fleet-wide health for the dashboard (ADR 0020): the reader merges every process's
    published metrics and the collectors; the publisher shares this API's own metrics."""
    if settings.database_url is None:
        return None
    engine = make_engine(settings.database_url.get_secret_value())
    publisher = SnapshotPublisher(MetricSnapshotStore(engine), "api")
    reader = build_reader(engine, settings.warehouse_path, own=("api", publisher.instance))
    return reader, publisher


def create_app(
    settings: Settings | None = None,
    forecast_service: ForecastProvider | None = None,
    webhook_receiver: WebhookReceiver | None = None,
    retry_handler: RetryHandler | None = None,
    incident_store: IncidentStore | None = None,
    system_view: tuple[SystemReader, SnapshotPublisher | None] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.service_name, settings.log_level.value)
    reader, publisher = system_view or build_system_view(settings) or (None, None)
    app = FastAPI(title="Praxis", version=__version__, lifespan=publisher_lifespan(publisher))
    app.include_router(forecast_router(forecast_service or build_forecast_service(settings)))
    app.include_router(webhook_router(webhook_receiver or build_webhook_receiver(settings)))
    app.include_router(
        ops_router(load_ops_config(), incident_store or build_incident_store(settings), reader)
    )
    token = settings.tasks_token.get_secret_value() if settings.tasks_token else None
    app.include_router(tasks_router(retry_handler or build_retry_handler(settings), token))

    install_middleware(app)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service=settings.service_name,
            version=__version__,
            environment=settings.environment.value,
        )

    return app
