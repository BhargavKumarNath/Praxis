"""Webhook receipt: verify -> parse -> route -> durable inbox insert -> acknowledge.

The receiver does no provider calls and no business processing (Stripe: "quickly return a
2xx response before any complex logic"). One INSERT into the Postgres inbox is the only side
effect; the insert is idempotent on the provider event id, so duplicate deliveries are
acknowledged as ``duplicate`` and never processed twice. Everything slow happens later in
``praxis.payments.processor``.

Routing uses current Stripe snapshot event names (docs.stripe.com/api/events/types and
docs.stripe.com/billing/subscriptions/webhooks, checked 2026-10-07). Unrouted types are
acknowledged as ``ignored`` and not stored: the endpoint should only be subscribed to the
routed types, and anything else carries no state Praxis models.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from praxis.payments.model import Notification, NotificationKind
from praxis.payments.signature import DEFAULT_TOLERANCE_S, verify
from praxis.tracing import (
    current_correlation_id,
    current_trace_id,
    new_correlation_id,
    new_trace_id,
)

logger = logging.getLogger(__name__)

PROVIDER = "stripe"
MAX_BODY_BYTES = 256 * 1024

# event type -> (kind of object to re-fetch, object type in data.object)
ROUTES: dict[str, tuple[NotificationKind, str]] = {
    **dict.fromkeys(
        (
            "invoice.created",
            "invoice.finalized",
            "invoice.updated",
            "invoice.paid",
            "invoice.payment_succeeded",
            "invoice.payment_failed",
            "invoice.payment_action_required",
            "invoice.marked_uncollectible",
            "invoice.voided",
        ),
        (NotificationKind.INVOICE, "invoice"),
    ),
    "invoice_payment.paid": (NotificationKind.INVOICE, "invoice_payment"),
    **dict.fromkeys(
        (
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
            "customer.subscription.paused",
            "customer.subscription.resumed",
        ),
        (NotificationKind.SUBSCRIPTION, "subscription"),
    ),
}
ROUTED_EVENT_TYPES = tuple(sorted(ROUTES))


class PayloadError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PayloadTooLarge(PayloadError):
    def __init__(self) -> None:
        super().__init__("payload_too_large")


@dataclass(frozen=True)
class ParsedEvent:
    event_id: str
    event_type: str
    created_at: datetime
    livemode: bool
    api_version: str | None
    object_id: str
    kind: NotificationKind | None

    def notification(self) -> Notification:
        assert self.kind is not None  # noqa: S101 - callers route first
        return Notification(
            provider=PROVIDER,
            event_id=self.event_id,
            event_type=self.event_type,
            kind=self.kind,
            object_id=self.object_id,
            created_at=self.created_at,
            livemode=self.livemode,
            api_version=self.api_version,
        )


def _str(value: object, prefix: str = "") -> str:
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) > 255:
        raise PayloadError("invalid_field")
    if len(value) <= len(prefix):
        raise PayloadError("invalid_field")
    return value


def parse_event(body: bytes) -> ParsedEvent:
    """Parse an already-verified Stripe event. Only routing fields are read."""
    try:
        doc: Any = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PayloadError("malformed_json") from None
    if not isinstance(doc, dict) or doc.get("object") != "event":
        raise PayloadError("not_an_event")
    event_type = _str(doc.get("type"))
    created = doc.get("created")
    livemode = doc.get("livemode")
    if type(created) is not int or created < 0 or type(livemode) is not bool:
        raise PayloadError("invalid_field")
    api_version = doc.get("api_version")
    data = doc.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    if not isinstance(obj, dict):
        raise PayloadError("missing_object")
    route = ROUTES.get(event_type)
    kind: NotificationKind | None = None
    object_id = ""  # unrouted events are acknowledged without reading their object
    if route is not None:
        kind, object_type = route
        if obj.get("object") != object_type:
            raise PayloadError("object_type_mismatch")
        field_name, prefix = ("invoice", "in_") if object_type == "invoice_payment" else ("id", "")
        object_id = _str(obj.get(field_name), prefix)
    return ParsedEvent(
        event_id=_str(doc.get("id"), "evt_"),
        event_type=event_type,
        created_at=datetime.fromtimestamp(created, UTC),
        livemode=livemode,
        api_version=api_version if isinstance(api_version, str) else None,
        object_id=object_id,
        kind=kind,
    )


class ReceiveStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"


@dataclass(frozen=True)
class ReceiveResult:
    status: ReceiveStatus
    event_id: str
    event_type: str


class InboxWriter(Protocol):
    def record(
        self, notification: Notification, *, body_sha256: str, trace_id: str, correlation_id: str
    ) -> bool:
        """Insert once per (provider, event id); True if new. May raise ``TransientError``."""
        ...


class WebhookReceiver:
    def __init__(
        self,
        inbox: InboxWriter,
        secrets: Sequence[str],
        *,
        tolerance_s: int = DEFAULT_TOLERANCE_S,
        max_body_bytes: int = MAX_BODY_BYTES,
        allow_livemode: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not secrets or any(not s for s in secrets):
            raise ValueError("at least one non-empty webhook signing secret is required")
        self._inbox = inbox
        self._secrets = tuple(secrets)
        self._tolerance_s = tolerance_s
        self._max_body = max_body_bytes
        self._allow_livemode = allow_livemode
        self._clock = clock

    def receive(self, body: bytes, signature_header: str | None) -> ReceiveResult:
        """Raises ``SignatureError``, ``PayloadError`` (reject) or ``TransientError`` (retry)."""
        if len(body) > self._max_body:
            raise PayloadTooLarge()
        verify(
            body, signature_header, self._secrets, now=self._clock(), tolerance_s=self._tolerance_s
        )
        parsed = parse_event(body)
        if parsed.livemode and not self._allow_livemode:
            raise PayloadError("livemode_not_allowed")
        if parsed.kind is None:
            logger.info(
                "webhook ignored",
                extra={"event_id": parsed.event_id, "event_type": parsed.event_type},
            )
            return ReceiveResult(ReceiveStatus.IGNORED, parsed.event_id, parsed.event_type)
        inserted = self._inbox.record(
            parsed.notification(),
            body_sha256=hashlib.sha256(body).hexdigest(),
            trace_id=current_trace_id() or new_trace_id(),
            correlation_id=current_correlation_id() or new_correlation_id(),
        )
        status = ReceiveStatus.ACCEPTED if inserted else ReceiveStatus.DUPLICATE
        logger.info(
            "webhook received",
            extra={
                "event_id": parsed.event_id,
                "event_type": parsed.event_type,
                "object_id": parsed.object_id,
                "status": status.value,
            },
        )
        return ReceiveResult(status, parsed.event_id, parsed.event_type)
