"""Streaming consumer: payment / churn events -> ``DunningService`` (subscription ``dunning``)."""

from __future__ import annotations

from praxis.dunning.service import RELEVANT, DunningEvent, DunningService
from praxis.events.codec import DecodedEvent
from praxis.payments.normalise import SOURCES
from praxis.streaming.topology import DUNNING
from praxis.streaming.transport import Delivery

_PROVIDER_BY_SOURCE = {source: provider for provider, source in SOURCES.items()}


def to_dunning_event(event: DecodedEvent, delivery_attempt: int = 1) -> DunningEvent:
    env = event.envelope
    return DunningEvent(
        event_id=str(env.event_id),
        event_type=env.event_type,
        entity_id=str(env.entity_id),
        occurred_at=env.occurred_at,
        payload=dict(env.payload),
        trace_id=env.trace_id,
        correlation_id=env.correlation_id,
        provider=_PROVIDER_BY_SOURCE.get(env.source),  # simulator events: not chargeable
        delivery_attempt=delivery_attempt,
    )


class DunningConsumer:
    name = DUNNING

    def __init__(self, service: DunningService) -> None:
        self.service = service

    def handle(self, event: DecodedEvent, delivery: Delivery) -> str:
        if event.envelope.event_type not in RELEVANT:
            return "ignored"
        return self.service.handle(to_dunning_event(event, delivery.delivery_attempt)).status
