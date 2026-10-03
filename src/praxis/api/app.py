"""Minimal FastAPI application: health endpoint plus correlation-ID middleware."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from praxis import __version__
from praxis.config import Settings, get_settings
from praxis.logging import configure_logging
from praxis.tracing import (
    CORRELATION_HEADER,
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.service_name, settings.log_level.value)
    app = FastAPI(title="Praxis", version=__version__)

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
