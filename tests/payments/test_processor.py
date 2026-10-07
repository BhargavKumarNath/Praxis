"""Asynchronous processor: coalescing, failure policy, and exactly-once effect under crashes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from praxis.control.store import ControlPlaneStore
from praxis.errors import PermanentError, TransientError
from praxis.payments.gateway import ForeignObject
from praxis.payments.model import (
    ChargeAttempt,
    ChargeStatus,
    InvoiceSnapshot,
    InvoiceStatus,
    Notification,
    NotificationKind,
    SubscriptionSnapshot,
    SubscriptionStatus,
)
from praxis.payments.processor import NotificationProcessor, ProcessReport
from praxis.payments.service import deliver_notifications
from praxis.payments.store import PostgresInbox
from praxis.streaming.pipeline import LocalPipeline
from tests.payments.conftest import _PipelinePublisher
from tests.payments.helpers import T0, control_state

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Source:
    """Scriptable provider read side: per-object snapshots or exceptions."""

    provider = "synthetic"

    def __init__(
        self, invoices: Mapping[str, Any] | None = None, subs: Mapping[str, Any] | None = None
    ) -> None:
        self.invoices = dict(invoices or {})
        self.subs = dict(subs or {})
        self.fetches: list[str] = []

    def _get(self, table: dict[str, Any], key: str) -> Any:
        self.fetches.append(key)
        value = table[key]
        if isinstance(value, BaseException):
            raise value
        return value

    def fetch_invoice(self, invoice_id: str) -> InvoiceSnapshot:
        return self._get(self.invoices, invoice_id)  # type: ignore[no-any-return]

    def fetch_subscription(self, subscription_id: str) -> SubscriptionSnapshot:
        return self._get(self.subs, subscription_id)  # type: ignore[no-any-return]


class Collect:
    def __init__(self, error: Exception | None = None) -> None:
        self.events: list[Mapping[str, Any]] = []
        self.error = error

    def publish(self, events: Sequence[Mapping[str, Any]], /) -> object:
        if self.error is not None:
            raise self.error
        self.events.extend(events)
        return None


def snapshot(invoice_id: str = "in_1", *charges: ChargeAttempt) -> InvoiceSnapshot:
    return InvoiceSnapshot(
        "synthetic",
        invoice_id,
        "cust_a",
        InvoiceStatus.OPEN,
        4900,
        "GBP",
        date(2026, 10, 1),
        date(2026, 11, 1),
        T0,
        "growth",
        charges,
    )


def notify(
    inbox: PostgresInbox,
    object_id: str,
    n: int = 1,
    kind: NotificationKind = NotificationKind.INVOICE,
) -> None:
    deliver_notifications(
        [
            Notification(
                "synthetic", f"evt_{object_id}_{i}", "invoice.updated", kind, object_id, T0
            )
            for i in range(n)
        ],
        inbox,
    )


def processor(
    inbox: PostgresInbox, source: Source, publisher: Any, clock: Clock | None = None, **kw: Any
) -> NotificationProcessor:
    return NotificationProcessor(
        inbox, {"synthetic": source}, publisher, clock=clock or Clock(), **kw
    )


def test_notifications_for_one_object_are_coalesced(inbox: PostgresInbox) -> None:
    notify(inbox, "in_1", n=5)
    notify(inbox, "in_2")
    source = Source({"in_1": snapshot("in_1"), "in_2": snapshot("in_2")})
    out = Collect()
    report = processor(inbox, source, out).run_once()
    assert (report.claimed, report.objects, report.processed, report.events_published) == (
        6,
        2,
        6,
        2,
    )
    assert sorted(source.fetches) == ["in_1", "in_2"]  # one fetch per object, not per event
    assert inbox.counts() == {"processed": 6}


def test_subscription_notifications_and_skips(inbox: PostgresInbox) -> None:
    sub = SubscriptionSnapshot(
        "synthetic",
        "sub_1",
        "cust_a",
        SubscriptionStatus.INCOMPLETE,
        "growth",
        ("api",),
        "new",
        4900,
        30,
        None,
    )
    notify(inbox, "sub_1", kind=NotificationKind.SUBSCRIPTION)
    report = processor(inbox, Source(subs={"sub_1": sub}), Collect()).run_once()
    assert report.skipped == {"not_activated": 1} and report.events_published == 0


def test_foreign_objects_are_ignored(inbox: PostgresInbox) -> None:
    notify(inbox, "in_x", n=2)
    report = processor(inbox, Source({"in_x": ForeignObject("not ours")}), Collect()).run_once()
    assert report.ignored == 2 and inbox.counts() == {"ignored": 2}


@pytest.mark.parametrize(
    "error",
    [PermanentError("bad", "request"), TypeError("schema drift")],
    ids=["permanent", "bug"],
)
def test_permanent_failures_fail_now_and_bugs_retry(inbox: PostgresInbox, error: Exception) -> None:
    notify(inbox, "in_1")
    report = processor(inbox, Source({"in_1": error}), Collect()).run_once()
    if isinstance(error, PermanentError):
        assert report.failed == 1 and inbox.counts() == {"failed": 1}
    else:
        assert report.retried == 1 and inbox.counts() == {"pending": 1}


def test_transient_failures_back_off_then_fail_after_max_attempts(inbox: PostgresInbox) -> None:
    notify(inbox, "in_1")
    clock = Clock()
    p = processor(
        inbox, Source({"in_1": TransientError("stripe 503")}), Collect(), clock, max_attempts=3
    )
    delays = []
    for _ in range(3):
        report = p.run_once()
        assert p.run_once().claimed == 0  # not due again before its backoff
        clock.now += timedelta(hours=1)
        delays.append((report.retried, report.failed))
    assert delays == [(1, 0), (1, 0), (0, 1)]
    assert inbox.counts() == {"failed": 1}
    assert [p.backoff(a).total_seconds() for a in (1, 2, 3, 20)] == [5, 10, 20, 900]


def test_publisher_failures(inbox: PostgresInbox) -> None:
    from praxis.events.codec import DecodeError
    from praxis.streaming.producer import ProducerContractError

    notify(inbox, "in_1")
    contract = Collect(ProducerContractError(0, DecodeError("invalid_payload", "x")))
    assert processor(inbox, Source({"in_1": snapshot()}), contract).run_once().failed == 1
    notify(inbox, "in_2")
    outage = Collect(TransientError("pubsub down"))
    assert processor(inbox, Source({"in_2": snapshot("in_2")}), outage).run_once().retried == 1


def test_unknown_provider_fails(inbox: PostgresInbox) -> None:
    notify(inbox, "in_1")
    report = NotificationProcessor(inbox, {}, Collect(), clock=Clock()).run_once()
    assert report.failed == 1


def test_drain_and_report_arithmetic(inbox: PostgresInbox) -> None:
    for i in range(7):
        notify(inbox, f"in_{i}")
    source = Source({f"in_{i}": snapshot(f"in_{i}") for i in range(7)})
    total = processor(inbox, source, Collect()).drain(limit=3)
    assert (total.claimed, total.objects, total.processed) == (7, 7, 7)
    combined = ProcessReport(skipped={"a": 1})
    combined.add(ProcessReport(claimed=2, skipped={"a": 2, "b": 1}))
    assert (combined.claimed, combined.skipped) == (2, {"a": 3, "b": 1})
    with pytest.raises(ValueError, match="max_attempts"):
        processor(inbox, source, Collect(), max_attempts=0)


def test_crash_after_publish_reprocesses_without_double_effect(
    inbox: PostgresInbox, pipeline: LocalPipeline, store: ControlPlaneStore
) -> None:
    """Worker dies after publishing but before marking: the lease expires, the object is
    re-fetched and re-published with the same event ids; the control plane applies once."""
    charges = (
        ChargeAttempt("ch_1", T0, ChargeStatus.FAILED, "card", "card_declined"),
        ChargeAttempt("ch_2", T0 + timedelta(hours=1), ChargeStatus.SUCCEEDED, "card"),
    )
    source = Source({"in_1": snapshot("in_1", *charges)})
    notify(inbox, "in_1")
    clock = Clock()

    class CrashingInbox:
        def __getattr__(self, name: str) -> Any:
            return getattr(inbox, name)

        def complete(self, *args: Any) -> int:
            raise SystemExit("worker killed")

    publisher = _PipelinePublisher(pipeline)
    with pytest.raises(SystemExit):
        crashing: Any = CrashingInbox()
        NotificationProcessor(crashing, {"synthetic": source}, publisher, clock=clock).run_once()
    clock.now += timedelta(minutes=2)  # lease expired
    report = processor(inbox, source, publisher, clock).run_once()
    assert report.processed == 1 and publisher.published == 10
    pipeline.drain()
    state = control_state(store, "cust_a")
    assert [(i["state"], i["attempts"]) for i in state["invoices"]] == [("paid", 2)]
    assert state["ledger"] == (1, 4900)
