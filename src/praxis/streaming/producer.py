"""Event producer: validate -> archive -> publish.

* Contract first: every event in a batch is validated (envelope and payload) before any
  side effect; one invalid event rejects the batch, so garbage never reaches the bus or
  the archive.
* Archive before publish: anything a consumer could ever see is replayable. If
  publishing fails after archiving, re-running the batch is safe (archive is
  content-addressed, consumers are idempotent).
* ``replay=True`` republishes archived events unchanged (same ``event_id``) with a
  ``praxis_replay`` attribute; consumers' idempotency makes replay safe.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from praxis.events.codec import ATTR_REPLAY, ATTR_SENT_AT, DecodeError, encode, validate_event
from praxis.streaming.archive import EventArchive
from praxis.streaming.transport import Publisher


class ProducerContractError(ValueError):
    def __init__(self, index: int, error: DecodeError) -> None:
        super().__init__(f"event #{index} rejected: {error}")
        self.index = index
        self.reason = error.reason


@dataclass(frozen=True)
class PublishReport:
    published: int
    archived_files: int


class EventProducer:
    def __init__(
        self,
        publisher: Publisher,
        topic: str,
        archive: EventArchive | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._publisher = publisher
        self._topic = topic
        self._archive = archive
        self._clock = clock

    def publish(
        self, events: Sequence[Mapping[str, Any]], *, replay: bool = False
    ) -> PublishReport:
        for i, event in enumerate(events):
            try:
                validate_event(event)
            except DecodeError as exc:
                raise ProducerContractError(i, exc) from None
        archived = 0
        if self._archive is not None and not replay:
            archived = self._archive.append(events).files_written
        messages = []
        for event in events:
            data, attrs = encode(event)
            attrs[ATTR_SENT_AT] = f"{self._clock():.6f}"
            if replay:
                attrs[ATTR_REPLAY] = "true"
            messages.append((data, attrs))
        self._publisher.publish_batch(self._topic, messages)
        return PublishReport(len(messages), archived)


def replay_archive(
    archive: EventArchive, producer: EventProducer, *, batch_size: int = 1000
) -> int:
    """Republish every archived event. Returns the number of events republished."""
    total = 0
    batch: list[dict[str, Any]] = []
    for event in archive.iter_events():
        batch.append(event)
        if len(batch) >= batch_size:
            total += producer.publish(batch, replay=True).published
            batch = []
    if batch:
        total += producer.publish(batch, replay=True).published
    return total
