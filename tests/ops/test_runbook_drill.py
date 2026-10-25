"""Runbook drill (Phase 11 gate): a real fault -> alert -> incident -> runbook procedure -> clear.

Fault: twelve events are dead-lettered (limit: 10). The `pubsub-backlog` runbook says to
redrive them; the drill executes that exact mitigation (`redrive_dead_letters`, the function
behind `python -m praxis.streaming dlq --redrive`) against Postgres and an in-memory broker,
then checks the Verify step: the alert clears and the incident auto-resolves.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine

from praxis.control.store import ControlPlaneStore, DeadLetterRecord
from praxis.events.codec import DecodedEvent, encode
from praxis.ops.config import REPO_ROOT, load_ops_config
from praxis.ops.incidents import (
    IncidentManager,
    IncidentStatus,
    PostgresIncidentStore,
    RecordingNotifier,
)
from praxis.ops.monitor import OpsMonitor
from praxis.ops.signals import sample_backlog
from praxis.streaming.memory import MemoryBroker
from praxis.streaming.metrics import StreamMetrics
from praxis.streaming.redrive import redrive_dead_letters
from praxis.streaming.runtime import ConsumerWorker, DeadLetterRouter
from praxis.streaming.topology import OPERATIONAL, build_topology
from praxis.streaming.transport import Delivery, Disposition
from praxis.telemetry import Telemetry
from tests.streaming.helpers import customer_lifecycle

TOPO = build_topology()
SUB = TOPO.by_role(OPERATIONAL)
T0 = datetime(2026, 10, 25, 12, tzinfo=UTC)


class _Applies:
    name = "applies"

    def __init__(self) -> None:
        self.applied: set[str] = set()

    def handle(self, event: DecodedEvent, delivery: Delivery) -> str:
        self.applied.add(event.event_id)
        return "applied"


def test_dead_letter_backlog_is_resolved_by_following_the_runbook(
    pg_engine: Engine, tel: Telemetry
) -> None:
    store = ControlPlaneStore(pg_engine)
    broker = MemoryBroker(TOPO)
    events = [customer_lifecycle(f"cust_{i}")[0] for i in range(12)]
    for e in events:
        body, _ = encode(e)
        store.record_dead_letter(
            DeadLetterRecord(SUB.name, "handler_error", "db down", e["event_id"],
                             e["event_type"], e["trace_id"], e["correlation_id"], 5, body)
        )  # fmt: skip

    notifier = RecordingNotifier()
    monitor = OpsMonitor(
        load_ops_config(),
        IncidentManager(PostgresIncidentStore(pg_engine), notifier, clear_after=2),
    )

    def observe() -> None:
        sample_backlog(
            tel, [SUB.name], broker.backlog, lambda _s: sum(store.dead_letter_counts().values())
        )

    # 1. detect: the alert fires and opens ONE incident that links the runbook
    observe()
    first = monitor.tick(T0)
    assert [a.rule_id for a in first.health.alerts()] == ["dlq_backlog_high"]
    (incident,) = first.incidents.opened
    assert incident.severity == "page" and incident.runbook == "docs/runbooks/pubsub-backlog.md"
    assert [e for e, _ in notifier.sent] == ["opened"]

    # 2. follow the runbook: its Mitigate step names the redrive command
    runbook = (REPO_ROOT / Path(incident.runbook)).read_text()
    assert "dlq --redrive" in runbook
    report = redrive_dead_letters(store, broker, TOPO.events_topic)  # that command's function
    assert report.republished == 12

    # 3. the system recovers: consumers apply the redriven events (idempotently)
    consumer = _Applies()
    worker = ConsumerWorker(
        consumer, DeadLetterRouter(broker, TOPO.dead_letter_topic), StreamMetrics()
    )
    while batch := broker.pull(SUB.name):
        assert set(worker.process(batch)) == {Disposition.ACK}
        broker.ack(SUB.name, [d.ack_id for d in batch])
    assert consumer.applied == {e["event_id"] for e in events}

    # 4. verify (runbook Verify step): backlog gone, alert clears, incident auto-resolves
    observe()
    assert monitor.tick(T0 + timedelta(minutes=1)).incidents.resolved == ()  # one clear: not yet
    done = monitor.tick(T0 + timedelta(minutes=2)).incidents.resolved
    assert [i.status for i in done] == [IncidentStatus.RESOLVED]
    timeline = PostgresIncidentStore(pg_engine).events(incident.incident_id)
    assert [e["kind"] for e in timeline] == ["opened", "auto_resolved"]
    assert [e for e, _ in notifier.sent] == ["opened", "resolved"]
