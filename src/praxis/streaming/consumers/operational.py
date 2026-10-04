"""Operational consumer: lifecycle events -> Postgres control plane (exactly-once effect)."""

from __future__ import annotations

from praxis.control.store import ControlPlaneStore, EventRecord
from praxis.events.codec import DecodedEvent
from praxis.streaming.topology import OPERATIONAL
from praxis.streaming.transport import Delivery


def to_record(event: DecodedEvent, delivery_attempt: int = 1) -> EventRecord:
    env = event.envelope
    return EventRecord(
        event_id=str(env.event_id),
        event_type=env.event_type,
        entity_id=env.entity_id,
        occurred_at=env.occurred_at,
        payload=env.payload,
        trace_id=env.trace_id,
        correlation_id=env.correlation_id,
        causation_id=str(env.causation_id) if env.causation_id else None,
        delivery_attempt=delivery_attempt,
    )


class OperationalConsumer:
    name = OPERATIONAL

    def __init__(self, store: ControlPlaneStore) -> None:
        self.store = store

    def handle(self, event: DecodedEvent, delivery: Delivery) -> str:
        result = self.store.apply_event(self.name, to_record(event, delivery.delivery_attempt))
        return result.status.value
