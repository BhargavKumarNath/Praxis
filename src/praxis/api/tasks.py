"""Retry-task endpoint: ``POST /v1/tasks/payment-retry`` (Cloud Tasks HTTP target).

Body ``{"job_id": "..."}`` only: the job row in Postgres says what to charge, so a caller can
at most trigger a retry that is already scheduled, due and still valid (``RetryExecutor``
guards). Protection: Cloud Run IAM with the task's OIDC token in the cloud, plus the shared
``X-Praxis-Task-Token`` (constant-time comparison) everywhere.

* 200: handled (succeeded, failed, cancelled, expired, stale): Cloud Tasks stops;
* 401: missing or wrong task token; 422: invalid body;
* 503: not configured, dispatched too early, or a transient failure: Cloud Tasks retries.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from praxis.dunning.executor import ExecResult, ExecStatus
from praxis.errors import TransientError
from praxis.tracing import current_correlation_id

logger = logging.getLogger(__name__)
TOKEN_HEADER = "X-Praxis-Task-Token"  # noqa: S105 - a header name, not a secret
RetryHandler = Callable[[str], ExecResult]


class RetryTaskBody(BaseModel):
    job_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class RetryTaskAck(BaseModel):
    status: str
    job_id: str


class TaskError(BaseModel):
    error: str
    correlation_id: str | None


def _error(status: int, reason: str) -> JSONResponse:
    body = TaskError(error=reason, correlation_id=current_correlation_id())
    return JSONResponse(status_code=status, content=body.model_dump())


def tasks_router(handler: RetryHandler | None, token: str | None) -> APIRouter:
    router = APIRouter(prefix="/v1/tasks", tags=["tasks"])

    @router.post(
        "/payment-retry",
        response_model=RetryTaskAck,
        responses={401: {"model": TaskError}, 503: {"model": TaskError}},
    )
    async def payment_retry(request: Request, body: RetryTaskBody) -> JSONResponse | RetryTaskAck:
        if handler is None or not token:
            return _error(503, "tasks_not_configured")
        supplied = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            logger.warning("retry task rejected: bad token")
            return _error(401, "invalid_task_token")
        try:
            result = await run_in_threadpool(handler, body.job_id)
        except TransientError:
            logger.warning("retry task deferred: transient failure", extra={"job_id": body.job_id})
            return _error(503, "transient_failure")
        if result.status is ExecStatus.TOO_EARLY:
            return _error(503, "too_early")
        return RetryTaskAck(status=result.status.value, job_id=result.job_id)

    return router
