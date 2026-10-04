"""DLQ inspector: records every dead letter in Postgres so it is visible and countable.

Dead letters come from two places: messages the runtime rejected as permanently invalid
(``praxis_dlq_*`` attributes) and messages Pub/Sub forwarded after
``max_delivery_attempts`` failures (``CloudPubSubDeadLetter*`` attributes). The body may be
unparseable, so this worker never decodes it as an event.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from praxis.control.store import ControlPlaneStore, DeadLetterRecord
from praxis.events.codec import (
    ATTR_CORRELATION_ID,
    ATTR_EVENT_ID,
    ATTR_EVENT_TYPE,
    ATTR_TRACE_ID,
)
from praxis.streaming.metrics import StreamMetrics
from praxis.streaming.runtime import DLQ_ATTEMPT, DLQ_DETAIL, DLQ_REASON, DLQ_SUBSCRIPTION
from praxis.streaming.topology import DLQ_INSPECT
from praxis.streaming.transport import (
    DEAD_LETTER_SOURCE_DELIVERY_COUNT,
    DEAD_LETTER_SOURCE_SUBSCRIPTION,
    Delivery,
    Disposition,
    TransientError,
)
from praxis.tracing import is_valid_correlation_id, is_valid_trace_id, trace_context

logger = logging.getLogger(__name__)
MAX_DELIVERY_ATTEMPTS_EXCEEDED = "max_delivery_attempts_exceeded"


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def to_dead_letter(delivery: Delivery) -> DeadLetterRecord:
    a: Mapping[str, str] = delivery.attributes
    forwarded = DEAD_LETTER_SOURCE_SUBSCRIPTION in a and DLQ_REASON not in a
    trace = a.get(ATTR_TRACE_ID)
    corr = a.get(ATTR_CORRELATION_ID)
    return DeadLetterRecord(
        source_subscription=(
            a.get(DLQ_SUBSCRIPTION) or a.get(DEAD_LETTER_SOURCE_SUBSCRIPTION) or "unknown"
        )[:256],
        reason=MAX_DELIVERY_ATTEMPTS_EXCEEDED if forwarded else a.get(DLQ_REASON, "unknown"),
        detail=a.get(DLQ_DETAIL),
        event_id=(a.get(ATTR_EVENT_ID) or None) and a[ATTR_EVENT_ID][:128],
        event_type=(a.get(ATTR_EVENT_TYPE) or None) and a[ATTR_EVENT_TYPE][:128],
        trace_id=trace if trace and is_valid_trace_id(trace) else None,
        correlation_id=corr if corr and is_valid_correlation_id(corr) else None,
        delivery_attempt=_int(a.get(DLQ_ATTEMPT) or a.get(DEAD_LETTER_SOURCE_DELIVERY_COUNT)),
        data=delivery.data,
    )


class DeadLetterWorker:
    name = DLQ_INSPECT

    def __init__(self, store: ControlPlaneStore, metrics: StreamMetrics) -> None:
        self.store = store
        self.metrics = metrics

    batches = False

    def process(self, deliveries: Sequence[Delivery]) -> list[Disposition]:
        return [self.process_one(d) for d in deliveries]

    def process_one(self, delivery: Delivery) -> Disposition:
        record = to_dead_letter(delivery)
        sub = delivery.subscription
        with trace_context(record.trace_id, record.correlation_id):
            try:
                new = self.store.record_dead_letter(record)
            except TransientError:
                self.metrics.inc(sub, "nack_transient")
                return Disposition.NACK
            self.metrics.inc(sub, "dead_letters_recorded" if new else "dead_letters_duplicate")
            if new:
                self.metrics.inc(sub, f"reason_{record.reason}")
                logger.warning(
                    "dead letter recorded",
                    extra={
                        "reason": record.reason,
                        "source_subscription": record.source_subscription,
                        "event_id": record.event_id,
                        "event_type": record.event_type,
                        "delivery_attempt": record.delivery_attempt,
                    },
                )
        return Disposition.ACK
