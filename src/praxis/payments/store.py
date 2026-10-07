"""Postgres repositories for payments: webhook inbox and provider object references.

Inbox lifecycle (migration 0003)::

    pending --claim (attempts+1, leased until now+lease)--> pending
    pending --complete--> processed      pending --ignore--> ignored
    pending --retry_later--> pending     pending --fail--> failed --requeue--> pending

A claim is a lease, not a lock held across provider calls: if the worker dies, the row
becomes due again when the lease expires and another worker re-processes it. That is safe
because processing derives deterministic event ids from provider state.

``MemoryRefStore`` has the same semantics as ``PostgresRefStore`` for offline use (the
synthetic gateway, unit tests).
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import Engine, text

from praxis.control.db import transient_errors
from praxis.payments.model import Notification, NotificationKind

InboxKey = tuple[str, str]  # (provider, provider event id)
MAX_ERROR_CHARS = 500


class RefConflict(RuntimeError):
    """A different provider object is already mapped to this Praxis key (or vice versa)."""


@dataclass(frozen=True)
class InboxItem:
    provider: str
    event_id: str
    event_type: str
    kind: NotificationKind
    object_id: str
    attempts: int
    trace_id: str
    correlation_id: str

    @property
    def key(self) -> InboxKey:
        return self.provider, self.event_id


class RefStore(Protocol):
    def get(self, provider: str, kind: str, praxis_key: str) -> str | None: ...

    def praxis_key_for(self, provider: str, kind: str, provider_id: str) -> str | None: ...

    def put(self, provider: str, kind: str, praxis_key: str, provider_id: str) -> None:
        """Idempotent for the same pair; ``RefConflict`` for a different one."""
        ...


class MemoryRefStore:
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str, str], str] = {}
        self._lock = threading.Lock()

    def get(self, provider: str, kind: str, praxis_key: str) -> str | None:
        return self._by_key.get((provider, kind, praxis_key))

    def praxis_key_for(self, provider: str, kind: str, provider_id: str) -> str | None:
        for (p, k, praxis_key), pid in self._by_key.items():
            if (p, k, pid) == (provider, kind, provider_id):
                return praxis_key
        return None

    def put(self, provider: str, kind: str, praxis_key: str, provider_id: str) -> None:
        with self._lock:
            existing = self._by_key.get((provider, kind, praxis_key))
            owner = self.praxis_key_for(provider, kind, provider_id)
            if existing not in (None, provider_id) or owner not in (None, praxis_key):
                raise RefConflict(f"{provider}/{kind}: {praxis_key} -> {provider_id} conflicts")
            self._by_key[(provider, kind, praxis_key)] = provider_id


class PostgresRefStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def get(self, provider: str, kind: str, praxis_key: str) -> str | None:
        with transient_errors(), self.engine.connect() as conn:
            value = conn.execute(
                text(
                    "SELECT provider_id FROM payment_provider_refs WHERE provider = :p "
                    "AND kind = :k AND praxis_key = :key"
                ),
                {"p": provider, "k": kind, "key": praxis_key},
            ).scalar_one_or_none()
        return None if value is None else str(value)

    def praxis_key_for(self, provider: str, kind: str, provider_id: str) -> str | None:
        with transient_errors(), self.engine.connect() as conn:
            value = conn.execute(
                text(
                    "SELECT praxis_key FROM payment_provider_refs WHERE provider = :p "
                    "AND kind = :k AND provider_id = :id"
                ),
                {"p": provider, "k": kind, "id": provider_id},
            ).scalar_one_or_none()
        return None if value is None else str(value)

    def put(self, provider: str, kind: str, praxis_key: str, provider_id: str) -> None:
        params = {"p": provider, "k": kind, "key": praxis_key, "id": provider_id}
        with transient_errors(), self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO payment_provider_refs (provider, kind, praxis_key, provider_id) "
                    "VALUES (:p, :k, :key, :id) ON CONFLICT DO NOTHING"
                ),
                params,
            )
            row = conn.execute(
                text(
                    "SELECT count(*) FILTER (WHERE praxis_key = :key AND provider_id = :id), "
                    "count(*) FROM payment_provider_refs WHERE provider = :p AND kind = :k "
                    "AND (praxis_key = :key OR provider_id = :id)"
                ),
                params,
            ).one()
        if int(row[0]) != 1 or int(row[1]) != 1:
            raise RefConflict(f"{provider}/{kind}: {praxis_key} -> {provider_id} conflicts")


_RECORD = text(
    "INSERT INTO payment_webhook_inbox (provider, provider_event_id, event_type, object_kind, "
    "object_id, provider_created_at, livemode, api_version, body_sha256, trace_id, "
    "correlation_id) VALUES (:provider, :event_id, :event_type, :kind, :object_id, :created, "
    ":livemode, :api_version, :sha, :trace_id, :correlation_id) "
    "ON CONFLICT (provider, provider_event_id) DO UPDATE "
    "SET deliveries = payment_webhook_inbox.deliveries + 1 RETURNING (xmax = 0) AS inserted"
)
_CLAIM = text(
    "WITH due AS (SELECT provider, provider_event_id FROM payment_webhook_inbox "
    "WHERE status = 'pending' AND next_attempt_at <= :now "
    "ORDER BY next_attempt_at, received_at, provider_event_id LIMIT :limit "
    "FOR UPDATE SKIP LOCKED) "
    "UPDATE payment_webhook_inbox i SET attempts = i.attempts + 1, next_attempt_at = :lease "
    "FROM due WHERE i.provider = due.provider AND i.provider_event_id = due.provider_event_id "
    "RETURNING i.provider, i.provider_event_id, i.event_type, i.object_kind, i.object_id, "
    "i.attempts, i.trace_id, i.correlation_id"
)
_KEYS = (
    "(provider, provider_event_id) IN "
    "(SELECT unnest(CAST(:providers AS TEXT[])), unnest(CAST(:ids AS TEXT[])))"
)


class PostgresInbox:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def record(
        self, notification: Notification, *, body_sha256: str, trace_id: str, correlation_id: str
    ) -> bool:
        n = notification
        with transient_errors(), self.engine.begin() as conn:
            inserted = conn.execute(
                _RECORD,
                {
                    "provider": n.provider,
                    "event_id": n.event_id,
                    "event_type": n.event_type,
                    "kind": n.kind.value,
                    "object_id": n.object_id,
                    "created": n.created_at,
                    "livemode": n.livemode,
                    "api_version": n.api_version,
                    "sha": body_sha256,
                    "trace_id": trace_id,
                    "correlation_id": correlation_id,
                },
            ).scalar_one()
        return bool(inserted)

    def claim(self, *, limit: int, now: datetime, lease: timedelta) -> list[InboxItem]:
        with transient_errors(), self.engine.begin() as conn:
            rows = conn.execute(_CLAIM, {"now": now, "limit": limit, "lease": now + lease}).all()
        return [
            InboxItem(
                str(r[0]),
                str(r[1]),
                str(r[2]),
                NotificationKind(r[3]),
                str(r[4]),
                int(r[5]),
                str(r[6]),
                str(r[7]),
            )
            for r in rows
        ]

    def _finish(self, keys: Sequence[InboxKey], assignments: str, params: dict[str, object]) -> int:
        if not keys:
            return 0
        # Only the fixed fragments of complete/ignore/retry_later/fail are interpolated.
        sql = text(
            f"UPDATE payment_webhook_inbox SET {assignments} WHERE status = 'pending' AND {_KEYS}"  # noqa: S608
        )
        with transient_errors(), self.engine.begin() as conn:
            result = conn.execute(
                sql, {**params, "providers": [k[0] for k in keys], "ids": [k[1] for k in keys]}
            )
        return int(result.rowcount)

    def complete(self, keys: Sequence[InboxKey], outcome: str, now: datetime) -> int:
        return self._finish(
            keys,
            "status = 'processed', processed_at = :now, outcome = :outcome, last_error = NULL",
            {"now": now, "outcome": outcome},
        )

    def ignore(self, keys: Sequence[InboxKey], reason: str, now: datetime) -> int:
        return self._finish(
            keys,
            "status = 'ignored', processed_at = :now, outcome = :reason",
            {"now": now, "reason": reason[:MAX_ERROR_CHARS]},
        )

    def retry_later(self, keys: Sequence[InboxKey], error: str, next_attempt_at: datetime) -> int:
        return self._finish(
            keys,
            "next_attempt_at = :at, last_error = :error",
            {"at": next_attempt_at, "error": error[:MAX_ERROR_CHARS]},
        )

    def fail(self, keys: Sequence[InboxKey], error: str, now: datetime) -> int:
        return self._finish(
            keys,
            "status = 'failed', processed_at = :now, last_error = :error",
            {"now": now, "error": error[:MAX_ERROR_CHARS]},
        )

    def requeue_failed(self, now: datetime) -> int:
        """Operator action after a fix: failed rows become due again with fresh attempts."""
        with transient_errors(), self.engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE payment_webhook_inbox SET status = 'pending', processed_at = NULL, "
                    "attempts = 0, next_attempt_at = :now WHERE status = 'failed'"
                ),
                {"now": now},
            )
        return int(result.rowcount)

    def counts(self) -> dict[str, int]:
        with transient_errors(), self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT status, count(*) FROM payment_webhook_inbox GROUP BY 1 ORDER BY 1")
            )
            return {str(r[0]): int(r[1]) for r in rows}

    def deliveries(self, provider: str, event_id: str) -> int | None:
        with transient_errors(), self.engine.connect() as conn:
            value = conn.execute(
                text(
                    "SELECT deliveries FROM payment_webhook_inbox WHERE provider = :p "
                    "AND provider_event_id = :e"
                ),
                {"p": provider, "e": event_id},
            ).scalar_one_or_none()
        return None if value is None else int(value)
