"""Consumer runtime: decode, bind trace context, classify failures, route to DLQ.

Failure policy (ADR 0009):

==============================  ===========================================  ==========
Failure                         Example                                      Outcome
==============================  ===========================================  ==========
decode error (permanent)        malformed JSON, unsupported schema version   DLQ now
``PermanentError``              consumer declares the event unprocessable    DLQ now
``TransientError``              database unavailable, timeout, deadlock      NACK
any other exception             handler bug, poison payload                  NACK, then
                                                                             broker DLQ
                                                                             after
                                                                             N attempts
``ConsumerCrashed``             process death (fault injection)              no ack
==============================  ===========================================  ==========

Retries are therefore bounded by the subscription's ``max_delivery_attempts``; there is
no retry loop in process. A message is only acked after the consumer's side effect is
durable, or after it was durably published to the DLQ.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from praxis.errors import PermanentError, TransientError
from praxis.events.codec import (
    ATTR_CORRELATION_ID,
    ATTR_EVENT_ID,
    ATTR_EVENT_TYPE,
    ATTR_SENT_AT,
    ATTR_TRACE_ID,
    DecodedEvent,
    DecodeError,
    decode,
)
from praxis.streaming.metrics import StreamMetrics
from praxis.streaming.transport import Delivery, Disposition, Publisher
from praxis.tracing import is_valid_correlation_id, is_valid_trace_id, trace_context

logger = logging.getLogger(__name__)

DLQ_REASON = "praxis_dlq_reason"
DLQ_DETAIL = "praxis_dlq_detail"
DLQ_SUBSCRIPTION = "praxis_dlq_subscription"
DLQ_ATTEMPT = "praxis_dlq_delivery_attempt"
_MAX_ATTR = 1000  # Pub/Sub attribute values are limited to 1024 bytes


class Consumer(Protocol):
    name: str

    def handle(self, event: DecodedEvent, delivery: Delivery) -> str:
        """Apply the event idempotently; return a status label (``applied``, ``duplicate``...)."""
        ...


class BatchConsumer(Consumer, Protocol):
    def handle_batch(self, events: Sequence[DecodedEvent]) -> str:
        """Apply all events atomically (all or nothing) and idempotently."""
        ...


class DeadLetterRouter:
    def __init__(self, publisher: Publisher, topic: str) -> None:
        self._publisher = publisher
        self._topic = topic

    def route(self, delivery: Delivery, reason: str, detail: str) -> None:
        attrs = dict(delivery.attributes)
        attrs[DLQ_REASON] = reason[:_MAX_ATTR]
        attrs[DLQ_DETAIL] = detail[:_MAX_ATTR]
        attrs[DLQ_SUBSCRIPTION] = delivery.subscription
        attrs[DLQ_ATTEMPT] = str(delivery.delivery_attempt)
        self._publisher.publish(self._topic, delivery.data, attrs)


def _inbound_ids(attrs: Mapping[str, str]) -> tuple[str | None, str | None]:
    trace = attrs.get(ATTR_TRACE_ID)
    corr = attrs.get(ATTR_CORRELATION_ID)
    return (
        trace if trace and is_valid_trace_id(trace) else None,
        corr if corr and is_valid_correlation_id(corr) else None,
    )


def transport_latency_ms(delivery: Delivery, now: float | None = None) -> float:
    """Wall-clock ms from producer send (or broker publish time) to now."""
    now = time.time() if now is None else now
    sent = delivery.attributes.get(ATTR_SENT_AT)
    try:
        start = float(sent) if sent is not None else delivery.publish_time.timestamp()
    except ValueError:
        start = delivery.publish_time.timestamp()
    return max(0.0, (now - start) * 1000.0)


class ConsumerWorker:
    """Runs one ``Consumer`` over deliveries from one subscription."""

    def __init__(
        self,
        consumer: Consumer,
        dead_letters: DeadLetterRouter,
        metrics: StreamMetrics,
        *,
        measure_transport_latency: bool = True,
    ) -> None:
        self.consumer = consumer
        self._dlq = dead_letters
        self._metrics = metrics
        self._measure_transport = measure_transport_latency

    @property
    def batches(self) -> bool:
        return hasattr(self.consumer, "handle_batch")

    def process(self, deliveries: Sequence[Delivery]) -> list[Disposition]:
        handle_batch = getattr(self.consumer, "handle_batch", None)
        if handle_batch is None or len(deliveries) < 2:
            return [self.process_one(d) for d in deliveries]
        return self._process_batch(deliveries, handle_batch)

    def _count(self, delivery: Delivery) -> None:
        self._metrics.inc(delivery.subscription, "deliveries")
        if delivery.delivery_attempt > 1:
            self._metrics.inc(delivery.subscription, "redeliveries")

    def _process_batch(
        self, deliveries: Sequence[Delivery], handle_batch: Callable[[list[DecodedEvent]], str]
    ) -> list[Disposition]:
        """Decode all, dead-letter the undecodable, apply the rest in one atomic call.

        A non-transient batch failure falls back to one-by-one processing so a single
        poison message is isolated instead of dragging its whole batch to the DLQ.
        """
        out: list[Disposition] = [Disposition.NACK] * len(deliveries)
        valid: list[tuple[int, Delivery, DecodedEvent]] = []
        for i, d in enumerate(deliveries):
            self._count(d)
            with trace_context(*_inbound_ids(d.attributes)):
                try:
                    valid.append((i, d, decode(d.data, d.attributes)))
                except DecodeError as exc:
                    out[i] = self._dead_letter(d, exc.reason, exc.detail)
        if not valid:
            return out
        sub = deliveries[0].subscription
        started = time.perf_counter()
        try:
            status = handle_batch([e for _, _, e in valid])
        except TransientError as exc:
            self._metrics.inc(sub, "nack_transient", len(valid))
            logger.warning("transient batch failure, will retry", extra={"error": str(exc)})
            return out
        except Exception as exc:
            self._metrics.inc(sub, "batch_fallback")
            logger.warning(
                "batch failed, isolating messages one by one",
                extra={"subscription": sub, "error_type": type(exc).__name__},
            )
            for i, d, _ in valid:
                out[i] = self.process_one(d, counted=True)
            return out
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._metrics.inc(sub, f"status_{status}", len(valid))
        self._metrics.inc(sub, "batches")
        for i, d, _ in valid:
            out[i] = Disposition.ACK
            self._metrics.observe(sub, "processing", elapsed_ms / len(valid))
            if self._measure_transport:
                self._metrics.observe(sub, "end_to_end", transport_latency_ms(d))
        return out

    def process_one(self, delivery: Delivery, *, counted: bool = False) -> Disposition:
        sub = delivery.subscription
        if not counted:
            self._count(delivery)
        started = time.perf_counter()
        trace_id, correlation_id = _inbound_ids(delivery.attributes)
        with trace_context(trace_id, correlation_id):
            try:
                event = decode(delivery.data, delivery.attributes)
            except DecodeError as exc:
                return self._dead_letter(delivery, exc.reason, exc.detail)
        with trace_context(event.trace_id, event.correlation_id):
            log = {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "subscription": sub,
                "delivery_attempt": delivery.delivery_attempt,
            }
            try:
                status = self.consumer.handle(event, delivery)
            except PermanentError as exc:
                return self._dead_letter(delivery, exc.reason, exc.detail)
            except TransientError as exc:
                self._metrics.inc(sub, "nack_transient")
                logger.warning("transient failure, will retry", extra={**log, "error": str(exc)})
                return Disposition.NACK
            except Exception as exc:
                self._metrics.inc(sub, "nack_error")
                logger.exception(
                    "handler failed, will retry until the DLQ policy triggers",
                    extra={**log, "error_type": type(exc).__name__},
                )
                return Disposition.NACK
            self._metrics.inc(sub, f"status_{status}")
            self._metrics.observe(sub, "processing", (time.perf_counter() - started) * 1000.0)
            if self._measure_transport:
                self._metrics.observe(sub, "end_to_end", transport_latency_ms(delivery))
            logger.debug("event processed", extra={**log, "status": status})
            return Disposition.ACK

    def _dead_letter(self, delivery: Delivery, reason: str, detail: str) -> Disposition:
        sub = delivery.subscription
        try:
            self._dlq.route(delivery, reason, detail)
        except Exception:
            # Could not make the DLQ copy durable: keep the original, retry later.
            self._metrics.inc(sub, "nack_dlq_publish_failed")
            logger.exception("dead-letter publish failed", extra={"subscription": sub})
            return Disposition.NACK
        self._metrics.inc(sub, "dead_lettered")
        self._metrics.inc(sub, f"dead_lettered_{reason}")
        logger.warning(
            "message dead-lettered",
            extra={
                "subscription": sub,
                "reason": reason,
                "event_id": delivery.attributes.get(ATTR_EVENT_ID),
                "event_type": delivery.attributes.get(ATTR_EVENT_TYPE),
                "delivery_attempt": delivery.delivery_attempt,
            },
        )
        return Disposition.ACK
