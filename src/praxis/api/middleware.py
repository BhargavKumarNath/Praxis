"""Cross-cutting HTTP behaviour shared by every Praxis FastAPI app (the core API and the
decision console): correlation ids, request telemetry, and error bodies that never expose
internals (CLAUDE.md s13).

Kept free of business imports so an app can install it without loading the core API's
routers (the console must not pull in payment or dunning execution paths).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from praxis.control.metric_snapshots import SnapshotPublisher
from praxis.telemetry import telemetry
from praxis.tracing import (
    CORRELATION_HEADER,
    current_correlation_id,
    is_valid_correlation_id,
    new_correlation_id,
    trace_context,
)

logger = logging.getLogger(__name__)


class ErrorResponse(BaseModel):
    error: str
    correlation_id: str
    detail: str | None = None


def record_request(request: Request, status: int, started: float) -> None:
    """Request count, 5xx count and latency, labelled by route TEMPLATE (bounded cardinality)."""
    route = getattr(request.scope.get("route"), "path", None) or "unmatched"
    tel = telemetry()
    tel.inc("http_requests_total", route=route, method=request.method, status=status)
    if status >= 500:
        tel.inc("http_server_errors_total", route=route)
    tel.observe("http_request_duration_ms", (time.perf_counter() - started) * 1000.0, route=route)


def install_middleware(app: FastAPI) -> None:
    """Correlation-id middleware, request telemetry and safe 422 / 500 bodies."""

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
        started = time.perf_counter()
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
            record_request(request, response.status_code, started)
            return response


def publisher_lifespan(
    publisher: SnapshotPublisher | None,
) -> Callable[[FastAPI], contextlib.AbstractAsyncContextManager[None]]:
    """App lifespan that publishes this process's metric snapshot on its interval (ADR 0020)."""

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = None
        if publisher is not None:

            async def publish_loop() -> None:
                while True:  # never raises on a database failure (SnapshotPublisher)
                    await asyncio.to_thread(publisher.publish)
                    await asyncio.sleep(publisher.interval.total_seconds())

            task = asyncio.create_task(publish_loop())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    return lifespan
