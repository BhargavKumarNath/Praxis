"""Event topology: topics, subscriptions, retry and dead-letter policy.

One definition drives the in-memory broker, the Pub/Sub emulator setup and (by a static
test) the Terraform module, so the three cannot drift apart silently.
"""

from __future__ import annotations

from dataclasses import dataclass

from praxis.events.codec import ATTR_STATEFUL

OPERATIONAL = "operational"
WAREHOUSE = "warehouse"
MONITORING = "monitoring"
DLQ_INSPECT = "dlq-inspect"


@dataclass(frozen=True, slots=True)
class SubscriptionSpec:
    name: str
    role: str
    topic: str
    ack_deadline_s: int
    min_backoff_s: float
    max_backoff_s: float
    max_delivery_attempts: int | None
    dead_letter_topic: str | None
    attribute_filter: tuple[tuple[str, str], ...] = ()

    def matches(self, attributes: dict[str, str] | None) -> bool:
        attrs = attributes or {}
        return all(attrs.get(k) == v for k, v in self.attribute_filter)

    def pubsub_filter(self) -> str:
        """Render the Pub/Sub filter expression (max 256 bytes, immutable after creation)."""
        expr = " AND ".join(f'attributes.{k} = "{v}"' for k, v in self.attribute_filter)
        if len(expr.encode()) > 256:
            raise ValueError("Pub/Sub filter expressions are limited to 256 bytes")
        return expr

    def backoff_s(self, attempt: int) -> float:
        """Exponential backoff after failed delivery ``attempt`` (1-based), capped."""
        exponent = min(max(0, attempt - 1), 32)  # unbounded subscriptions keep retrying
        return float(min(self.max_backoff_s, self.min_backoff_s * 2**exponent))


@dataclass(frozen=True, slots=True)
class Topology:
    events_topic: str
    dead_letter_topic: str
    subscriptions: tuple[SubscriptionSpec, ...]

    def by_role(self, role: str) -> SubscriptionSpec:
        for sub in self.subscriptions:
            if sub.role == role:
                return sub
        raise KeyError(role)

    @property
    def topics(self) -> tuple[str, str]:
        return self.events_topic, self.dead_letter_topic


def build_topology(
    prefix: str = "praxis",
    environment: str = "local",
    *,
    ack_deadline_s: int = 30,
    min_backoff_s: float = 10.0,
    max_backoff_s: float = 300.0,
    max_delivery_attempts: int = 5,
) -> Topology:
    if not 5 <= max_delivery_attempts <= 100:
        raise ValueError("Pub/Sub requires max_delivery_attempts between 5 and 100")
    if not 0 <= min_backoff_s <= max_backoff_s <= 600:
        raise ValueError("backoff must satisfy 0 <= min <= max <= 600 s")
    if not 10 <= ack_deadline_s <= 600:
        raise ValueError("ack deadline must be 10-600 s")
    base = f"{prefix}-{environment}-events"
    dlq = f"{base}-dlq"

    def sub(role: str, *flt: tuple[str, str]) -> SubscriptionSpec:
        return SubscriptionSpec(
            name=f"{base}-{role}",
            role=role,
            topic=base,
            ack_deadline_s=ack_deadline_s,
            min_backoff_s=min_backoff_s,
            max_backoff_s=max_backoff_s,
            max_delivery_attempts=max_delivery_attempts,
            dead_letter_topic=dlq,
            attribute_filter=tuple(flt),
        )

    return Topology(
        events_topic=base,
        dead_letter_topic=dlq,
        subscriptions=(
            # Only state-changing events reach Postgres; bulk usage never does (ADR 0002).
            sub(OPERATIONAL, (ATTR_STATEFUL, "true")),
            sub(WAREHOUSE),
            sub(MONITORING),
            SubscriptionSpec(
                name=f"{dlq}-inspect",
                role=DLQ_INSPECT,
                topic=dlq,
                ack_deadline_s=ack_deadline_s,
                min_backoff_s=min_backoff_s,
                max_backoff_s=max_backoff_s,
                max_delivery_attempts=None,  # the DLQ itself has no DLQ
                dead_letter_topic=None,
            ),
        ),
    )
