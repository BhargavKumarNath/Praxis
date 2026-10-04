"""Monitoring consumer: per-event-type delivery telemetry and trace-annotated logs.

It owns no business state. Duplicate detection uses a bounded in-memory LRU, so it is
best-effort and resets on restart; that is acceptable because these are delivery
metrics. Business counts come from the idempotent warehouse, never from here.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from praxis.events.codec import DecodedEvent
from praxis.streaming.metrics import StreamMetrics
from praxis.streaming.topology import MONITORING
from praxis.streaming.transport import Delivery

logger = logging.getLogger(__name__)


class MonitoringConsumer:
    name = MONITORING

    def __init__(self, metrics: StreamMetrics, *, dedupe_capacity: int = 100_000) -> None:
        self.metrics = metrics
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._capacity = dedupe_capacity

    def handle(self, event: DecodedEvent, delivery: Delivery) -> str:
        eid = event.event_id
        sub = delivery.subscription
        if eid in self._seen:
            self._seen.move_to_end(eid)
            self.metrics.inc(sub, "duplicate_deliveries")
            return "duplicate"
        self._seen[eid] = None
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        self.metrics.inc(sub, "unique_events_observed")
        self.metrics.inc(sub, f"type_{event.event_type}")
        logger.debug(
            "event observed",
            extra={
                "event_id": eid,
                "event_type": event.event_type,
                "entity_id": event.envelope.entity_id,
                "delivery_attempt": delivery.delivery_attempt,
            },
        )
        return "observed"
