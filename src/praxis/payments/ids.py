"""Deterministic identifiers for payment flows.

* Internal event ids are UUIDv5 over ``provider:kind:object:role``: re-deriving events from
  the same provider state always yields the same ids, so the control plane's
  ``processed_events`` table deduplicates re-processing, duplicate webhooks and replays.
* Outbound idempotency keys are a hash of the business intent (operation + intent parts),
  never random: the retry of an intent reuses its key by construction.
"""

from __future__ import annotations

import hashlib
import uuid

# Fixed namespace for payment-derived identifiers. Changing it changes every event id.
PAYMENTS_NAMESPACE = uuid.UUID("5d0c7a52-8a46-4b8e-9c55-2c3f0f5b7a01")
_MAX_KEY = 255  # Stripe's idempotency-key length limit


def derived_event_id(provider: str, kind: str, object_id: str, role: str) -> str:
    for part in (provider, kind, object_id, role):
        if not part or ":" in part:  # ":" would make two different tuples collide
            raise ValueError(f"invalid id component {part!r}")
    return str(uuid.uuid5(PAYMENTS_NAMESPACE, f"{provider}:{kind}:{object_id}:{role}"))


def flow_correlation_id(provider: str, kind: str, object_id: str) -> str:
    """One correlation id per business flow (an invoice's collection, a customer's billing)."""
    return str(uuid.uuid5(PAYMENTS_NAMESPACE, f"flow:{provider}:{kind}:{object_id}"))


def idempotency_key(operation: str, *intent: str) -> str:
    # NUL frames the parts, so it may not occur inside one (("a\0b",) vs ("a", "b")).
    if not operation.isidentifier() or not intent or any(not p or "\0" in p for p in intent):
        raise ValueError("idempotency key needs an operation name and non-empty intent parts")
    digest = hashlib.sha256("\0".join(intent).encode()).hexdigest()[:40]
    key = f"praxis-{operation}-{digest}"
    if len(key) > _MAX_KEY:
        raise ValueError("operation name too long for an idempotency key")
    return key
