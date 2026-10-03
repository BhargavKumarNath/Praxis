"""Deterministic event construction and canonical serialisation.

Event, trace and correlation IDs are pure functions of ``(run_id, key)``, never of
iteration order or a global counter, so replays are byte-identical. ``published_at``
equals ``occurred_at``: late arrival is not modelled in Phase 1 (Phase 3 adds delivery
faults), so no entity timestamp ever moves backwards.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

SOURCE = "simulator"


@dataclass(frozen=True, slots=True)
class Draft:
    """An event before IDs and timestamps are rendered."""

    ts: int  # epoch seconds
    entity_id: str
    uid: str  # unique within a run; identity of the event
    event_type: str
    payload: dict[str, Any]
    flow: str  # business flow shared by related events (trace / correlation)
    cause_uid: str | None = None


def _digest(text: str) -> bytes:
    return hashlib.blake2b(text.encode(), digest_size=16).digest()


def iso_utc(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class EventFactory:
    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._flow_ids = lru_cache(maxsize=65536)(self._flow_ids_uncached)

    def event_id(self, uid: str) -> str:
        return str(uuid.UUID(bytes=_digest(f"{self._run_id}:e:{uid}"), version=4))

    def _flow_ids_uncached(self, flow: str) -> tuple[str, str]:
        corr = str(uuid.UUID(bytes=_digest(f"{self._run_id}:c:{flow}"), version=4))
        trace = _digest(f"{self._run_id}:t:{flow}").hex()
        return trace, corr

    def build(self, d: Draft) -> dict[str, Any]:
        trace, corr = self._flow_ids(d.flow)
        when = iso_utc(d.ts)
        return {
            "schema_version": 1,
            "event_id": self.event_id(d.uid),
            "event_type": d.event_type,
            "source": SOURCE,
            "occurred_at": when,
            "published_at": when,
            "trace_id": trace,
            "correlation_id": corr,
            "causation_id": self.event_id(d.cause_uid) if d.cause_uid else None,
            "entity_id": d.entity_id,
            "is_synthetic": True,
            "payload": d.payload,
        }


def canonical_line(event: dict[str, Any]) -> bytes:
    return (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
