"""Asynchronous webhook processing: inbox -> provider re-fetch -> internal events -> bus.

For each claimed batch the processor groups notifications by provider object, fetches the
object's *current* state once (Stripe's guidance for out-of-order events: treat the event as
a signal and retrieve the object), derives the internal events (``normalise``) and publishes
them through the Phase 3 producer. Only then are the inbox rows marked processed.

Exactly-once *effect*, not exactly-once processing: a crash after publishing and before
marking re-processes the object later, re-deriving the same event ids, which the control
plane deduplicates (``processed_events``). Failure policy:

=====================================  ==============================================
failure                                outcome
=====================================  ==============================================
``ForeignObject``                      ``ignored`` (object not created by Praxis)
``TransientError`` (429, 5xx, DB)      retry with exponential backoff, ``failed`` after
                                       ``max_attempts``
``PermanentError``, producer contract  ``failed`` now (needs a fix, then ``requeue``)
any other exception (bug)              logged with traceback, retried like a transient
                                       failure so a bounded number of attempts is made
=====================================  ==============================================
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from praxis.errors import PermanentError, TransientError
from praxis.payments.gateway import ForeignObject, SnapshotSource
from praxis.payments.model import NotificationKind
from praxis.payments.normalise import Derived, invoice_events, subscription_events
from praxis.payments.store import InboxItem, InboxKey
from praxis.streaming.producer import ProducerContractError
from praxis.tracing import trace_context

logger = logging.getLogger(__name__)


class EventPublisher(Protocol):
    def publish(self, events: Sequence[Mapping[str, Any]], /) -> object:
        """Validate and durably publish; raises on contract errors or transport failure."""
        ...


class Inbox(Protocol):
    def claim(self, *, limit: int, now: datetime, lease: timedelta) -> list[InboxItem]: ...

    def complete(self, keys: Sequence[InboxKey], outcome: str, now: datetime) -> int: ...

    def ignore(self, keys: Sequence[InboxKey], reason: str, now: datetime) -> int: ...

    def retry_later(
        self, keys: Sequence[InboxKey], error: str, next_attempt_at: datetime
    ) -> int: ...

    def fail(self, keys: Sequence[InboxKey], error: str, now: datetime) -> int: ...


@dataclass
class ProcessReport:
    claimed: int = 0
    objects: int = 0
    events_published: int = 0
    processed: int = 0
    ignored: int = 0
    retried: int = 0
    failed: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def add(self, other: ProcessReport) -> None:
        for name in (
            "claimed",
            "objects",
            "events_published",
            "processed",
            "ignored",
            "retried",
            "failed",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for reason, n in other.skipped.items():
            self.skipped[reason] = self.skipped.get(reason, 0) + n


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NotificationProcessor:
    def __init__(  # noqa: PLR0913 - dependencies and bounded-retry policy, all explicit
        self,
        inbox: Inbox,
        sources: Mapping[str, SnapshotSource],
        publisher: EventPublisher,
        *,
        clock: Callable[[], datetime] = _utcnow,
        max_attempts: int = 8,
        lease: timedelta = timedelta(seconds=60),
        backoff_base: timedelta = timedelta(seconds=5),
        backoff_cap: timedelta = timedelta(minutes=15),
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._inbox = inbox
        self._sources = dict(sources)
        self._publisher = publisher
        self._clock = clock
        self._max_attempts = max_attempts
        self._lease = lease
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap

    def backoff(self, attempts: int) -> timedelta:
        return min(self._backoff_cap, self._backoff_base * (1 << max(0, attempts - 1)))

    def run_once(self, limit: int = 100) -> ProcessReport:
        report = ProcessReport()
        items = self._inbox.claim(limit=limit, now=self._clock(), lease=self._lease)
        report.claimed = len(items)
        groups: dict[tuple[str, NotificationKind, str], list[InboxItem]] = {}
        for item in items:
            groups.setdefault((item.provider, item.kind, item.object_id), []).append(item)
        for (provider, kind, object_id), group in groups.items():
            report.objects += 1
            first = group[0]
            with trace_context(first.trace_id, first.correlation_id):
                self._process_object(provider, kind, object_id, group, report)
        return report

    def drain(self, *, limit: int = 100, max_rounds: int = 1000) -> ProcessReport:
        """Process until nothing is due (rows waiting for a backoff are left for later)."""
        total = ProcessReport()
        for _ in range(max_rounds):
            report = self.run_once(limit)
            total.add(report)
            if report.claimed == 0:
                break
        return total

    def _derive(
        self, provider: str, kind: NotificationKind, object_id: str, trace_id: str
    ) -> Derived:
        try:
            source = self._sources[provider]
        except KeyError:
            raise PermanentError("unknown_provider", provider) from None
        now = self._clock()
        if kind is NotificationKind.INVOICE:
            return invoice_events(
                source.fetch_invoice(object_id), published_at=now, trace_id=trace_id
            )
        return subscription_events(
            source.fetch_subscription(object_id), published_at=now, trace_id=trace_id
        )

    def _process_object(
        self,
        provider: str,
        kind: NotificationKind,
        object_id: str,
        group: list[InboxItem],
        report: ProcessReport,
    ) -> None:
        keys = [i.key for i in group]
        attempts = max(i.attempts for i in group)
        log = {"provider": provider, "object_kind": kind.value, "object_id": object_id}
        try:
            derived = self._derive(provider, kind, object_id, group[0].trace_id)
            if derived.events:
                self._publisher.publish(derived.events)
        except ForeignObject as exc:
            report.ignored += self._inbox.ignore(keys, exc.reason, self._clock())
            logger.info("payment object ignored", extra={**log, "reason": exc.reason})
            return
        except (PermanentError, ProducerContractError) as exc:  # derived events break a contract
            report.failed += self._inbox.fail(keys, str(exc), self._clock())
            logger.error("payment object failed", extra={**log, "reason": exc.reason})
            return
        except TransientError as exc:
            self._retry_or_fail(keys, attempts, f"transient: {exc}", report, log)
            return
        except Exception as exc:  # a bug or poison object: bounded attempts, never a hot loop
            logger.exception("payment object processing error", extra=log)
            self._retry_or_fail(keys, attempts, f"error: {type(exc).__name__}", report, log)
            return
        outcome = f"events={len(derived.events)}" + (
            f" skipped={derived.skipped}" if derived.skipped else ""
        )
        report.processed += self._inbox.complete(keys, outcome, self._clock())
        report.events_published += len(derived.events)
        if derived.skipped:
            report.skipped[derived.skipped] = report.skipped.get(derived.skipped, 0) + 1
        logger.info(
            "payment object processed",
            extra={**log, "events": len(derived.events), "deliveries": len(group)},
        )

    def _retry_or_fail(
        self,
        keys: list[InboxKey],
        attempts: int,
        error: str,
        report: ProcessReport,
        log: dict[str, str],
    ) -> None:
        now = self._clock()
        if attempts >= self._max_attempts:
            report.failed += self._inbox.fail(keys, error, now)
            logger.error("payment object failed after retries", extra={**log, "attempts": attempts})
            return
        report.retried += self._inbox.retry_later(keys, error, now + self.backoff(attempts))
        logger.warning("payment object retry scheduled", extra={**log, "attempts": attempts})
