"""Stripe webhook endpoint: ``POST /v1/webhooks/stripe``.

The handler reads the raw body bytes (never a parsed-and-reserialised JSON object, which
would break the signature), hands them to ``WebhookReceiver`` and answers as soon as the
event is durably in the inbox. Responses:

* 200 ``accepted`` / ``duplicate`` / ``ignored``: Stripe stops retrying;
* 400 invalid signature or payload, 413 too large: Stripe retries (a few times in a sandbox),
  which surfaces misconfiguration in the dashboard; nothing is stored;
* 503 inbox unavailable or webhooks not configured: Stripe retries later.

Error bodies carry a reason code and the correlation id, never the payload or a traceback.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from praxis.errors import TransientError
from praxis.payments.signature import SIGNATURE_HEADER, SignatureError
from praxis.payments.webhook import PayloadError, PayloadTooLarge, WebhookReceiver
from praxis.tracing import current_correlation_id

logger = logging.getLogger(__name__)

_BAD_REQUEST = 400
_TOO_LARGE = 413
_UNAVAILABLE = 503


class WebhookAck(BaseModel):
    status: str
    event_id: str
    event_type: str


class WebhookError(BaseModel):
    error: str
    correlation_id: str | None


def _error(status: int, reason: str) -> JSONResponse:
    body = WebhookError(error=reason, correlation_id=current_correlation_id())
    return JSONResponse(status_code=status, content=body.model_dump())


def webhook_router(receiver: WebhookReceiver | None) -> APIRouter:
    router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

    @router.post(
        "/stripe",
        response_model=WebhookAck,
        responses={
            400: {"model": WebhookError},
            413: {"model": WebhookError},
            503: {"model": WebhookError},
        },
    )
    async def stripe_webhook(request: Request) -> JSONResponse | WebhookAck:
        if receiver is None:
            return _error(_UNAVAILABLE, "webhooks_not_configured")
        body = await request.body()
        try:
            # One small INSERT; run it off the event loop so slow I/O never blocks others.
            result = await run_in_threadpool(
                receiver.receive, body, request.headers.get(SIGNATURE_HEADER)
            )
        except SignatureError as exc:
            logger.warning("webhook signature rejected", extra={"reason": exc.reason})
            return _error(_BAD_REQUEST, f"invalid_signature:{exc.reason}")
        except PayloadTooLarge:
            return _error(_TOO_LARGE, "payload_too_large")
        except PayloadError as exc:
            logger.warning("webhook payload rejected", extra={"reason": exc.reason})
            return _error(_BAD_REQUEST, f"invalid_payload:{exc.reason}")
        except TransientError:
            logger.warning("webhook inbox unavailable")
            return _error(_UNAVAILABLE, "inbox_unavailable")
        return WebhookAck(
            status=result.status.value, event_id=result.event_id, event_type=result.event_type
        )

    return router
