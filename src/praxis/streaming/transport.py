"""Transport-neutral message types shared by the in-memory broker and Pub/Sub."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
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


ISOLATE_FROM_ATTEMPT = 3  # a message delivered this often has been redelivered twice


class FlowControl:
    """How many messages one consumer process may hold leased at once (ADR 0025).

    A crashed process cannot ack, so every message it held loses one delivery attempt, and
    the broker dead-letters a message after ``max_delivery_attempts`` failed leases whatever
    their cause. So the window follows the evidence of crashes:

    * slow start: a new process (which cannot tell a first start from a restart after a
      crash) leases one message and doubles after every pull it completes, up to
      ``max_messages``; a crash starts it over at one;
    * isolation: a message redelivered twice or more (``delivery_attempt >=
      isolate_from``) is processed alone and drops the window to one, so it is leased
      alone next time. A first redelivery is weak evidence (a crash redelivers every
      message the process held, and nacks redeliver too), so it is batched with the other
      first redeliveries and does not shrink the window: recovery from a crash or an outage
      keeps its throughput.
    """

    def __init__(self, max_messages: int, isolate_from: int = ISOLATE_FROM_ATTEMPT) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be >= 1")
        if isolate_from < 2:
            raise ValueError("isolate_from must be >= 2 (first deliveries are never suspects)")
        self.max_messages = max_messages
        self.isolate_from = isolate_from
        self.limit = 1

    def restart(self) -> None:
        """The process died: its replacement starts over."""
        self.limit = 1

    def completed(self, deliveries: Sequence[Delivery]) -> None:
        """A pull finished without a crash: size the next one by the evidence it held."""
        if any(d.delivery_attempt >= self.isolate_from for d in deliveries):
            self.limit = 1
        else:
            self.limit = min(self.max_messages, self.limit * 2)

    def groups(self, deliveries: Sequence[Delivery], batches: bool) -> list[list[Delivery]]:
        """Processing groups of one pull, each acked as soon as it completes.

        First deliveries, then first redeliveries (one atomic group each for a batch
        worker), then every repeatedly redelivered message alone, least-retried first.
        """
        tiers: tuple[list[Delivery], list[Delivery]] = ([], [])
        isolated: list[Delivery] = []
        for d in deliveries:
            if d.delivery_attempt >= self.isolate_from:
                isolated.append(d)
            else:
                tiers[d.delivery_attempt > 1].append(d)
        out: list[list[Delivery]] = []
        for tier in tiers:
            if tier:
                out.extend([tier] if batches else [[d] for d in tier])
        return out + [[d] for d in sorted(isolated, key=lambda d: d.delivery_attempt)]


class Tally(Protocol):
    acks: int
    nacks: int


def process_pull(
    worker: Worker,
    flow: FlowControl,
    deliveries: Sequence[Delivery],
    ack: Callable[[list[str]], object],
    nack: Callable[[list[str]], object],
    tally: Tally,
) -> None:
    """Run one pull through ``worker`` group by group, counting into ``tally`` as each
    group is acked, then size the next pull. May raise ``ConsumerCrashed``: earlier groups
    stay acked, and the caller restarts the worker and ``flow``."""
    for group in flow.groups(deliveries, worker.batches):
        dispositions = worker.process(group)
        pairs = list(zip(group, dispositions, strict=True))
        acks = [d.ack_id for d, s in pairs if s is Disposition.ACK]
        nacks = [d.ack_id for d, s in pairs if s is Disposition.NACK]
        ack(acks)
        nack(nacks)
        tally.acks += len(acks)
        tally.nacks += len(nacks)
    flow.completed(deliveries)


class ConsumerCrashed(BaseException):
    """Fault injection: the consumer process died. Nothing is acked or nacked.

    Derives from ``BaseException`` so ordinary ``except Exception`` handlers cannot swallow
    it, exactly like a killed process cannot run its handlers.
    """
