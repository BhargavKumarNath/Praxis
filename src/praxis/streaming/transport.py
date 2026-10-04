"""Transport-neutral message types shared by the in-memory broker and Pub/Sub."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

# Attributes Pub/Sub adds when its dead-letter policy forwards a message.
DEAD_LETTER_SOURCE_SUBSCRIPTION = "CloudPubSubDeadLetterSourceSubscription"
DEAD_LETTER_SOURCE_DELIVERY_COUNT = "CloudPubSubDeadLetterSourceDeliveryCount"


class Disposition(StrEnum):
    ACK = "ack"
    NACK = "nack"


@dataclass(frozen=True, slots=True)
class Delivery:
    """One delivery attempt of one message to one subscription."""

    subscription: str
    message_id: str
    ack_id: str
    data: bytes
    attributes: Mapping[str, str]
    publish_time: datetime
    delivery_attempt: int = 1
    received_monotonic: float = field(default=0.0, compare=False)


class Publisher(Protocol):
    def publish(self, topic: str, data: bytes, attributes: Mapping[str, str]) -> str:
        """Publish one message durably; returns the transport message id."""
        ...

    def publish_batch(
        self, topic: str, messages: Sequence[tuple[bytes, Mapping[str, str]]]
    ) -> list[str]:
        """Publish many messages; returns only after every one is durable."""
        ...


class Worker(Protocol):
    @property
    def batches(self) -> bool:
        """True: process a pulled batch atomically. False: ack each message as it completes."""
        ...

    def process(self, deliveries: Sequence[Delivery]) -> list[Disposition]:
        """One disposition per delivery, same order. May raise ``ConsumerCrashed``."""
        ...


class TransientError(RuntimeError):
    """A failure that may succeed on redelivery (database down, timeout, deadlock)."""


class PermanentError(RuntimeError):
    """A failure that can never succeed for this message; dead-letter immediately."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class ConsumerCrashed(BaseException):
    """Fault injection: the consumer process died. Nothing is acked or nacked.

    Derives from ``BaseException`` so ordinary ``except Exception`` handlers cannot swallow
    it, exactly like a killed process cannot run its handlers.
    """
