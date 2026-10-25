"""Incident flow: dedup, touch, auto-resolve, manual actions; memory and Postgres stores agree."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine

from praxis.control.db import make_engine
from praxis.ops.config import load_ops_config
from praxis.ops.evaluate import Alert
from praxis.ops.incidents import (
    Incident,
    IncidentError,
    IncidentManager,
    IncidentStatus,
    IncidentStore,
    LogNotifier,
    MemoryIncidentStore,
    PostgresIncidentStore,
    RecordingNotifier,
)
from praxis.ops.monitor import synthetic_alert

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def alert(rule: str = "queue_backlog_high", observed: float = 2000.0) -> Alert:
    return Alert(rule, "service", "page", "backlog", "pubsub-backlog", observed, 1000.0,
                 {"subscription": "operational"})  # fmt: skip


@pytest.fixture(params=["memory", "postgres"])
def store(
    request: pytest.FixtureRequest, pg_url_factory: Callable[[], str]
) -> Iterator[IncidentStore]:
    if request.param == "memory":
        yield MemoryIncidentStore()
        return
    engine: Engine = make_engine(pg_url_factory())
    yield PostgresIncidentStore(engine)
    engine.dispose()


def manager(store: IncidentStore, **kw: int) -> tuple[IncidentManager, RecordingNotifier]:
    notifier = RecordingNotifier()
    return IncidentManager(store, notifier, **kw), notifier


def test_alert_opens_one_incident_and_notifies_once(store: IncidentStore) -> None:
    mgr, notifier = manager(store)
    first = mgr.process([alert()], T0)
    again = mgr.process([alert(observed=3000.0)], T0 + timedelta(minutes=1))
    assert len(first.opened) == 1 and first.touched == () and len(again.touched) == 1
    (live,) = store.live()
    assert live.occurrences == 2 and live.context["observed"] == 3000.0
    assert live.runbook == "docs/runbooks/pubsub-backlog.md" and live.status is IncidentStatus.OPEN
    assert [e for e, _ in notifier.sent] == ["opened"]
    assert [e["kind"] for e in store.events(live.incident_id)] == ["opened"]


def test_incident_auto_resolves_after_consecutive_clear_evaluations(store: IncidentStore) -> None:
    mgr, notifier = manager(store, clear_after=2)
    mgr.process([alert()], T0)
    assert mgr.process([], T0 + timedelta(minutes=1)).resolved == ()  # one clear: not yet
    mgr.process([alert()], T0 + timedelta(minutes=2))  # fires again: streak resets
    assert mgr.process([], T0 + timedelta(minutes=3)).resolved == ()
    (done,) = mgr.process([], T0 + timedelta(minutes=4)).resolved
    assert done.status is IncidentStatus.RESOLVED and done.resolved_at == T0 + timedelta(minutes=4)
    assert store.live() == [] and [e for e, _ in notifier.sent] == ["opened", "resolved"]
    assert [e["kind"] for e in store.events(done.incident_id)] == ["opened", "auto_resolved"]


def test_a_new_incident_opens_after_resolution(store: IncidentStore) -> None:
    mgr, _ = manager(store, clear_after=1)
    first = mgr.process([alert()], T0).opened[0]
    mgr.process([], T0 + timedelta(minutes=1))
    second = mgr.process([alert()], T0 + timedelta(minutes=2)).opened[0]
    assert second.incident_id != first.incident_id
    assert len(store.recent()) == 2 and len(store.live()) == 1


def test_acknowledge_and_manual_resolve_are_attributed(store: IncidentStore) -> None:
    mgr, _ = manager(store)
    inc = mgr.process([alert()], T0).opened[0]
    acked = mgr.acknowledge(inc.incident_id, "oncall", T0 + timedelta(minutes=1))
    assert acked.status is IncidentStatus.ACKNOWLEDGED and acked.acknowledged_by == "oncall"
    assert (
        mgr.process([alert()], T0 + timedelta(minutes=2)).touched[0].status
        is IncidentStatus.ACKNOWLEDGED
    )
    done = mgr.resolve(inc.incident_id, "oncall", T0 + timedelta(minutes=3))
    assert done.status is IncidentStatus.RESOLVED
    kinds = [e["kind"] for e in store.events(inc.incident_id)]
    assert kinds == ["opened", "acknowledged", "resolved"]
    with pytest.raises(IncidentError):
        mgr.acknowledge(inc.incident_id, "oncall")
    with pytest.raises(IncidentError):
        mgr.resolve("inc_unknown", "oncall")


def test_distinct_alerts_get_distinct_incidents(store: IncidentStore) -> None:
    mgr, _ = manager(store)
    report = mgr.process([alert(), alert("dlq_backlog_high")], T0)
    assert {i.rule_id for i in report.opened} == {"queue_backlog_high", "dlq_backlog_high"}


def test_duplicate_create_is_refused_not_duplicated(store: IncidentStore) -> None:
    mgr, _ = manager(store)
    inc = mgr.process([alert()], T0).opened[0]
    assert store.create(inc) is False
    assert len(store.live()) == 1


def test_notifier_failure_is_recorded_and_the_incident_survives(
    store: IncidentStore, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken:
        def notify(self, event: str, incident: object) -> None:
            raise RuntimeError("pager down")

    mgr = IncidentManager(store, Broken())
    with caplog.at_level(logging.ERROR):
        report = mgr.process([alert()], T0)
    (live,) = store.live()
    assert report.opened == (live,) or report.opened[0].incident_id == live.incident_id
    assert [e["kind"] for e in store.events(live.incident_id)] == ["opened", "notify_failed"]
    assert "incident notification failed" in caplog.text


def test_synthetic_alert_opens_a_labelled_incident_with_the_real_runbook(
    store: IncidentStore,
) -> None:
    cfg = load_ops_config()
    mgr, notifier = manager(store)
    mgr.process([synthetic_alert(cfg, c) for c in ("service", "data", "model", "business")], T0)
    incidents = store.live()
    assert len(incidents) == 4 and all(i.synthetic for i in incidents)
    assert all(i.summary.startswith("[SYNTHETIC]") for i in incidents)
    assert all(i.runbook.startswith("docs/runbooks/") for i in incidents)
    assert len(notifier.sent) == 4


def test_log_notifier_emits_a_structured_record(caplog: pytest.LogCaptureFixture) -> None:
    mgr = IncidentManager(MemoryIncidentStore(), LogNotifier())
    with caplog.at_level(logging.WARNING):
        mgr.process([alert()], T0)
    rec = next(r for r in caplog.records if r.message == "incident.opened")
    assert rec.levelno == logging.ERROR and rec.__dict__["runbook"].endswith("pubsub-backlog.md")


def test_clear_after_must_be_positive() -> None:
    with pytest.raises(ValueError, match="clear_after"):
        IncidentManager(MemoryIncidentStore(), RecordingNotifier(), clear_after=0)


def test_manual_open_creates_or_touches_without_clearing_others(store: IncidentStore) -> None:
    mgr, sent = manager(store)
    mgr.process([alert("dlq_backlog_high")], T0)
    first, created = mgr.open(alert(), T0)
    again, created_again = mgr.open(alert(), T0 + timedelta(minutes=1))
    assert created and not created_again and again.incident_id == first.incident_id
    assert again.occurrences == 2
    other = next(i for i in store.live() if i.rule_id == "dlq_backlog_high")
    assert other.clear_streak == 0  # a manual open is not an evaluation tick
    assert [e for e, _ in sent.sent] == ["opened", "opened"]


def test_manual_open_gives_up_when_the_store_never_admits_it() -> None:
    class Stubborn(MemoryIncidentStore):
        def create(self, incident: Incident) -> bool:
            return False  # a racing opener "won", yet its incident is not visible

    mgr = IncidentManager(Stubborn(), RecordingNotifier())
    with pytest.raises(IncidentError, match="could not open"):
        mgr.open(alert(), T0)
