"""DLQ redrive: republish stored dead letters once the underlying fault is fixed.

An operator action, never automatic: a dead letter means "a human should look". Only
messages whose bytes decode as a valid event are republished (a malformed message would
just dead-letter again); they keep their ``event_id``, so every consumer's idempotency
makes redrive safe even for subscriptions that had already processed the event.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from praxis.control.store import ControlPlaneStore
from praxis.events.codec import ATTR_SENT_AT, DecodeError, decode, encode
from praxis.streaming.transport import Publisher

logger = logging.getLogger(__name__)
ATTR_REDRIVE = "praxis_redrive"


@dataclass(frozen=True)
class RedriveReport:
    republished: int
    skipped_undecodable: int


def redrive_dead_letters(
    store: ControlPlaneStore,
    publisher: Publisher,
    topic: str,
    *,
    reason: str | None = None,
    limit: int = 1000,
    clock: Callable[[], float] = time.time,
) -> RedriveReport:
    republished: list[str] = []
    skipped = 0
    messages = []
    for key, data in store.dead_letters_for_redrive(reason=reason, limit=limit):
        try:
            event = decode(data)
        except DecodeError:
            skipped += 1
            continue
        body, attrs = encode(event.envelope.model_dump(mode="json"))
        attrs[ATTR_REDRIVE] = "true"
        attrs[ATTR_SENT_AT] = f"{clock():.6f}"  # latency counts from the redrive, not the original
        messages.append((body, attrs))
        republished.append(key)
    if messages:
        publisher.publish_batch(topic, messages)
    store.mark_redriven(republished)
    logger.warning(
        "dead letters redriven",
        extra={"republished": len(republished), "skipped_undecodable": skipped, "reason": reason},
    )
    return RedriveReport(len(republished), skipped)
