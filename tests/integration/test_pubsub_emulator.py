"""Integration: producer + consumers over the real Pub/Sub API (emulator, no cloud, no cost).

Runs in ``make test`` and the CI backend job (both start the emulator and set
``PUBSUB_EMULATOR_HOST``, so these tests count towards coverage); ``make pubsub-verify`` runs
only this module. Each test gets its own topology (unique environment name), so
tests never see each other's messages.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import google.cloud.pubsub_v1 as pubsub_v1
import pytest

from praxis.control.store import ControlPlaneStore
from praxis.data.warehouse import Warehouse
from praxis.events.codec import encode
from praxis.streaming.consumers.dead_letter import DeadLetterWorker
from praxis.streaming.consumers.monitoring import MonitoringConsumer
from praxis.streaming.consumers.operational import OperationalConsumer
from praxis.streaming.consumers.warehouse import WarehouseConsumer
from praxis.streaming.metrics import StreamMetrics
from praxis.streaming.pipeline import expected_state, observed_state
from praxis.streaming.producer import EventProducer
from praxis.streaming.pubsub import (
    EMULATOR_ENV,
    PubSubPublisher,
    PubSubPuller,
    ensure_topology,
    run_pull_loop,
)
from praxis.streaming.runtime import Consumer, ConsumerWorker, DeadLetterRouter
from praxis.streaming.topology import (
    DLQ_INSPECT,
    MONITORING,
    OPERATIONAL,
    WAREHOUSE,
    Topology,
    build_topology,
)
from praxis.streaming.transport import Worker
from tests.streaming.helpers import (
    command_output,
    customer_lifecycle,
    invoice_lifecycle,
    sim_events,
)

EMULATOR = os.environ.get(EMULATOR_ENV)
pytestmark = [
    pytest.mark.pubsub_emulator,
    pytest.mark.skipif(
        not EMULATOR, reason=f"needs {EMULATOR_ENV}; run `make test` or `make pubsub-verify`"
    ),
]
PROJECT = "praxis-local"
FLOW = customer_lifecycle(changes=1, churn=False) + invoice_lifecycle(fail_first=1)


@pytest.fixture(autouse=True)
def _emulator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert EMULATOR is not None
    monkeypatch.setenv(EMULATOR_ENV, EMULATOR)


@pytest.fixture
def topo() -> Topology:
    t = build_topology("praxis", f"it{uuid.uuid4().hex[:8]}", min_backoff_s=0, max_backoff_s=1)
    assert len(ensure_topology(PROJECT, t)) == 6
    return t


@pytest.fixture
def warehouse() -> Iterator[Warehouse]:
    w = Warehouse(None)
    w.migrate()
    yield w
    w.close()


class Harness:
    def __init__(self, topo: Topology, store: ControlPlaneStore, warehouse: Warehouse) -> None:
        self.topo = topo
        self.store = store
        self.warehouse = warehouse
        self.metrics = StreamMetrics()
        self.publisher = PubSubPublisher(PROJECT)
        self.producer = EventProducer(self.publisher, topo.events_topic)

    def worker(self, role: str, consumer: Consumer | None = None) -> Worker:
        if role == DLQ_INSPECT:
            return DeadLetterWorker(self.store, self.metrics)
        default: dict[str, Consumer] = {
            OPERATIONAL: OperationalConsumer(self.store),
            WAREHOUSE: WarehouseConsumer(self.warehouse),
            MONITORING: MonitoringConsumer(self.metrics),
        }
        router = DeadLetterRouter(self.publisher, self.topo.dead_letter_topic)
        return ConsumerWorker(consumer or default[role], router, self.metrics)

    def consume(self, role: str, consumer: Consumer | None = None, idle: float = 3.0) -> int:
        puller = PubSubPuller(PROJECT, self.topo.by_role(role).name)
        report = run_pull_loop(puller, self.worker(role, consumer), idle_timeout_s=idle)
        puller.close()
        return report.deliveries

    def consume_all(self) -> None:
        for role in (OPERATIONAL, WAREHOUSE, MONITORING, DLQ_INSPECT):
            self.consume(role)


@pytest.fixture
def h(topo: Topology, store: ControlPlaneStore, warehouse: Warehouse) -> Harness:
    return Harness(topo, store, warehouse)


def test_topology_setup_is_idempotent(topo: Topology) -> None:
    assert ensure_topology(PROJECT, topo) == []
    sub = pubsub_v1.SubscriberClient()
    got = sub.get_subscription(
        request={"subscription": sub.subscription_path(PROJECT, topo.by_role(OPERATIONAL).name)}
    )
    assert got.filter == 'attributes.stateful = "true"'
    assert got.dead_letter_policy.max_delivery_attempts == 5
    sub.close()


def test_refuses_to_create_cloud_topology_without_emulator(
    topo: Topology, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(EMULATOR_ENV)
    with pytest.raises(RuntimeError, match="Terraform"):
        ensure_topology(PROJECT, topo)


def test_end_to_end_with_duplicates_matches_oracle(h: Harness) -> None:
    events = list(sim_events(40, 14))
    h.producer.publish(events)
    h.producer.publish(events[: len(events) // 3])  # producer-side duplicates
    h.consume_all()
    snap = h.store.snapshot()
    assert observed_state(snap) == expected_state(events, 3)
    assert h.warehouse.count("raw.sim_events") == len(events)
    stateful = sum(1 for e in events if encode(e)[1]["stateful"] == "true")
    ops = h.topo.by_role(OPERATIONAL).name
    # The subscription filter delivered only stateful events to the control plane.
    assert h.metrics.count(ops, "deliveries") == stateful + sum(
        1 for e in events[: len(events) // 3] if encode(e)[1]["stateful"] == "true"
    )
    assert h.store.dead_letter_counts() == {}
    latency = h.metrics.snapshot()[ops]["latency"]
    assert latency["end_to_end"]["count"] > 0


def test_malformed_and_unsupported_messages_reach_dlq(h: Harness) -> None:
    good_data, attrs = encode(FLOW[0])
    bad = json.loads(good_data)
    bad["schema_version"] = 2
    h.publisher.publish(h.topo.events_topic, json.dumps(bad).encode(), attrs)
    h.publisher.publish(h.topo.events_topic, b"{not json", {"stateful": "true"})
    h.producer.publish(FLOW)
    h.consume_all()
    assert observed_state(h.store.snapshot()) == expected_state(FLOW, 3)
    counts = h.store.dead_letter_counts()
    # Each bad message is rejected by all three consumer subscriptions.
    assert counts == {"unsupported_schema_version": 3, "malformed_json": 3}


class AlwaysFails:
    name = OPERATIONAL

    def __init__(self) -> None:
        self.calls = 0

    def handle(self, *_: object) -> str:
        self.calls += 1
        raise RuntimeError("poison")


def test_poison_message_is_forwarded_by_pubsub_after_bounded_attempts(h: Harness) -> None:
    h.producer.publish(FLOW[:1])
    poison = AlwaysFails()
    h.consume(OPERATIONAL, poison, idle=5.0)
    assert poison.calls == 5  # Pub/Sub's dead-letter policy bounded the retries
    h.consume(DLQ_INSPECT)
    with h.store.engine.connect() as conn:
        from sqlalchemy import text

        row = conn.execute(
            text("SELECT reason, delivery_attempt, source_subscription, event_id, trace_id "
                 "FROM dead_letters")
        ).one()  # fmt: skip
    assert tuple(row) == (
        "max_delivery_attempts_exceeded",
        5,
        h.topo.by_role(OPERATIONAL).name,
        FLOW[0]["event_id"],
        FLOW[0]["trace_id"],
    )


def test_unacked_messages_are_redelivered_after_crash_like_nack(h: Harness) -> None:
    h.producer.publish(FLOW)
    puller = PubSubPuller(PROJECT, h.topo.by_role(OPERATIONAL).name)
    first = puller.pull(100, timeout_s=5.0)
    assert first
    worker = h.worker(OPERATIONAL)
    worker.process(first)  # side effects committed ...
    puller.nack([d.ack_id for d in first])  # ... but the process "died" before acking
    puller.close()
    h.consume_all()
    snap = h.store.snapshot()
    assert observed_state(snap) == expected_state(FLOW, 3)
    assert snap["ledger_entries"] == 1
    assert h.metrics.count(h.topo.by_role(OPERATIONAL).name, "status_duplicate") >= len(first)


def test_cli_against_emulator(
    pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every emulator CLI command, end to end, on a fresh topology."""
    from praxis.streaming.__main__ import main

    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    monkeypatch.chdir(tmp_path)
    env = [
        "--database-url",
        pg_url,
        "--environment",
        f"cli{uuid.uuid4().hex[:6]}",
        "--min-backoff",
        "0",
    ]
    try:
        assert main([*env, "emulator-setup"]) == 0
        assert len(command_output(capsys.readouterr().out)["created"]) == 6
        archive = tmp_path / "archive"
        assert (
            main([*env, "produce", "--customers", "10", "--days", "7", "--archive", str(archive)])
            == 0
        )
        published = command_output(capsys.readouterr().out)["published"]
        for role in ("operational", "warehouse", "monitoring", "dlq-inspect"):
            duck = ["--duckdb", str(tmp_path / "wh.duckdb")] if role == "warehouse" else []
            assert main([*env, "consume", "--role", role, "--idle-timeout", "3", *duck]) == 0
        capsys.readouterr()
        assert main([*env, "replay", "--archive", str(archive)]) == 0
        assert command_output(capsys.readouterr().out)["replayed"] == published
        assert main([*env, "consume", "--role", "operational", "--idle-timeout", "3"]) == 0
        capsys.readouterr()
        assert main([*env, "dlq", "--redrive"]) == 0
        out = command_output(capsys.readouterr().out)
        assert out == {
            "open_dead_letters": {},
            "redrive": {"republished": 0, "skipped_undecodable": 0},
        }
        report = tmp_path / "bench.json"
        assert main(
            [*env, "emulator-bench", "--customers", "10", "--days", "7", "--rate", "0",
             "--idle-timeout", "3", "--report", str(report)]
        ) == 0  # fmt: skip
        assert json.loads(report.read_text())["matches_oracle"] is True
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
