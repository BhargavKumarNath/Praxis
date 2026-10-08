"""FastAPI application: health, demand forecasts, Stripe webhooks, correlation-ID middleware."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from praxis import __version__
from praxis.api.forecast import build_forecast_service, forecast_router
from praxis.api.tasks import RetryHandler, tasks_router
from praxis.api.webhooks import webhook_router
from praxis.config import Settings, get_settings
from praxis.control.db import make_engine
from praxis.forecasting.service import ForecastService
from praxis.logging import configure_logging
from praxis.payments.store import PostgresInbox
from praxis.payments.webhook import WebhookReceiver
from praxis.tracing import (
    CORRELATION_HEADER,
    current_correlation_id,
    is_valid_correlation_id,
    new_correlation_id,
    trace_context,
)

logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str


class ErrorResponse(BaseModel):
    error: str
    correlation_id: str
    detail: str | None = None


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


def create_app(
    settings: Settings | None = None,
    forecast_service: ForecastService | None = None,
    webhook_receiver: WebhookReceiver | None = None,
    retry_handler: RetryHandler | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.service_name, settings.log_level.value)
    app = FastAPI(title="Praxis", version=__version__)
    app.include_router(forecast_router(forecast_service or build_forecast_service(settings)))
    app.include_router(webhook_router(webhook_receiver or build_webhook_receiver(settings)))
    token = settings.tasks_token.get_secret_value() if settings.tasks_token else None
    app.include_router(tasks_router(retry_handler or build_retry_handler(settings), token))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations and messages only: never echo internals or the raw input back.
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', 'invalid')}"
            for err in exc.errors()[:10]
        )
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error="invalid_request",
                correlation_id=current_correlation_id() or new_correlation_id(),
                detail=detail,
            ).model_dump(),
        )

    @app.middleware("http")
    async def correlation_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        inbound = request.headers.get(CORRELATION_HEADER)
        correlation_id = inbound if inbound and is_valid_correlation_id(inbound) else None
        correlation_id = correlation_id or new_correlation_id()
        with trace_context(correlation_id=correlation_id) as (_trace_id, cid):
            try:
                response = await call_next(request)
            except Exception:
                logger.exception("unhandled error", extra={"path": request.url.path})
                response = JSONResponse(
                    status_code=500,
                    content=ErrorResponse(error="internal_error", correlation_id=cid).model_dump(),
                )
            response.headers[CORRELATION_HEADER] = cid
            return response

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service=settings.service_name,
            version=__version__,
            environment=settings.environment.value,
        )

    return app
