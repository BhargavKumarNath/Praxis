"""Phase 15 failures 4-6 and 12: Pub/Sub redelivery, consumer crash, service restart, burst.

Real boundaries: the Pub/Sub emulator (redelivery after a lost ack), Postgres (control plane,
incidents, webhook inbox), DuckDB (warehouse consumer), the production consumers and
``LocalPipeline`` (the deterministic in-memory broker that ``make event-chaos`` uses) for
seeded crashes and the burst. The oracle is always ``pipeline.expected_state`` (the Phase 1
validator fed every event once, in order). Worlds: seed 1, fault seed 801 (pinned config).
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from praxis.api.app import create_app
from praxis.config import Settings
from praxis.control.db import make_engine
from praxis.control.store import ControlPlaneStore, DeadLetterRecord
from praxis.data.warehouse import Warehouse
from praxis.events.codec import encode
from praxis.ops.collect import dead_letter_collector
from praxis.ops.incidents import PostgresIncidentStore
from praxis.ops.signals import sample_backlog
from praxis.ops.system import SystemReader
from praxis.payments.processor import NotificationProcessor
from praxis.payments.store import PostgresInbox
from praxis.payments.webhook import WebhookReceiver
from praxis.streaming.consumers.operational import OperationalConsumer
from praxis.streaming.faults import CrashPlan
from praxis.streaming.memory import FaultPlan, MemoryBroker
from praxis.streaming.metrics import StreamMetrics
from praxis.streaming.pipeline import LocalPipeline, expected_state, observed_state
from praxis.streaming.producer import EventProducer
from praxis.streaming.pubsub import (
    EMULATOR_ENV,
    PubSubPublisher,
    PubSubPuller,
    ensure_topology,
    run_pull_loop,
)
from praxis.streaming.redrive import redrive_all
from praxis.streaming.runtime import ConsumerWorker, DeadLetterRouter
from praxis.streaming.topology import OPERATIONAL, WAREHOUSE, build_topology
from praxis.telemetry import Telemetry
from tests.payments.helpers import body_of, signed, stripe_event, stripe_invoice, webhook_secret
from tests.payments.test_processor import Clock, Collect, Source, snapshot
from tests.resilience.harness import CRITERIA, Measured, OpsProbe, detection, judge, quiet_fired
from tests.streaming.helpers import customer_lifecycle, invoice_lifecycle, sim_events

RUN = CRITERIA.run
EMULATOR = os.environ.get(EMULATOR_ENV)
MAX_ATTEMPTS = 3  # pipeline.expected_state: invoices failing more often stay unpaid
POISON_FLOWS = 12  # consumer_crash storm (configs/resilience/remediation.toml)


@pytest.fixture
def warehouse() -> Iterator[Warehouse]:
    w = Warehouse(None)
    w.migrate()
    yield w
    w.close()


def _stateful(events: Sequence[dict[str, Any]]) -> set[str]:
    return {e["event_id"] for e in events if encode(e)[1]["stateful"] == "true"}


def _processed(store: ControlPlaneStore) -> tuple[set[str], int]:
    with store.engine.connect() as conn:
        rows = [r[0] for r in conn.execute(text("SELECT event_id::text FROM processed_events"))]
    return set(rows), len(rows)


def _dead_letters(store: ControlPlaneStore) -> list[tuple[str, str]]:
    with store.engine.connect() as conn:
        rows = conn.execute(text("SELECT source_subscription, event_id::text FROM dead_letters"))
        return [(r[0], r[1]) for r in rows]


def _dlq_reader(store: ControlPlaneStore) -> SystemReader:
    return SystemReader(None, {"dead_letters": dead_letter_collector(store)})


# 4 --------------------------------------------------------------------------------------
@pytest.mark.pubsub_emulator
@pytest.mark.skipif(not EMULATOR, reason=f"needs {EMULATOR_ENV} (make resilience / make test)")
def test_pubsub_redelivery(
    store: ControlPlaneStore, monkeypatch: pytest.MonkeyPatch, tel: Telemetry
) -> None:
    fid = "pubsub_redelivery"
    assert EMULATOR is not None
    monkeypatch.setenv(EMULATOR_ENV, EMULATOR)
    topo = build_topology("praxis", f"p15{uuid.uuid4().hex[:8]}", ack_deadline_s=10,
                          min_backoff_s=0, max_backoff_s=1)  # fmt: skip
    ensure_topology("praxis-local", topo)
    events = [dict(e) for e in sim_events(40, 14, seed=RUN.sim_seed)]
    publisher = PubSubPublisher("praxis-local")
    producer = EventProducer(publisher, topo.events_topic)
    sub = topo.by_role(OPERATIONAL).name
    metrics = StreamMetrics()
    router = DeadLetterRouter(publisher, topo.dead_letter_topic)
    probe = OpsProbe(_dlq_reader(store))
    producer.publish(events)
    probe.tick()

    # Inject 1: a consumer applies a batch and dies before acking (no ack, no nack).
    probe.mark()
    puller = PubSubPuller("praxis-local", sub)
    lost = puller.pull(100, timeout_s=5.0)
    ConsumerWorker(OperationalConsumer(store), router, metrics).process(lost)
    puller.close()
    # Inject 2: the publisher retries a batch it already sent (e.g. after a timeout).
    retried = events[: len(events) // 4]
    producer.publish(retried)
    injected = len(lost) + len(_stateful(retried))

    # A restarted consumer drains; the lost-ack copies come back after the ack deadline.
    started = time.perf_counter()
    puller = PubSubPuller("praxis-local", sub)
    loop = run_pull_loop(puller, ConsumerWorker(OperationalConsumer(store), router, metrics),
                         idle_timeout_s=topo.by_role(OPERATIONAL).ack_deadline_s + 5.0)  # fmt: skip
    puller.close()
    drain_s = time.perf_counter() - started
    probe.tick()
    clear = probe.until_clear((), max_ticks=1)

    snap = store.snapshot()
    matches = observed_state(snap) == expected_state(events, MAX_ATTEMPTS)
    expected = _stateful(events)
    ids, rows = _processed(store)
    duplicates = metrics.count(sub, "status_duplicate")
    record = judge(Measured(
        fid,
        actual=(f"{len(lost)} deliveries applied then their ack lost; {len(_stateful(retried))} "
                f"stateful events re-published; restarted consumer saw {loop.deliveries} "
                f"deliveries in {drain_s:.1f} s, {duplicates} recognised as duplicates"),
        detection_ticks={}, detection_wall_ms={}, quiet_fired=quiet_fired(probe, fid),
        recovered=matches and store.dead_letter_counts() == {},
        recovery_actual=f"state == oracle: {matches}",
        clear_ticks=clear, data_loss=len(expected - ids), duplicate_side_effects=rows - len(ids),
        signal_observed=duplicates, signal_expected=injected,
        evidence={"ack_deadline_s": topo.by_role(OPERATIONAL).ack_deadline_s,
                  "stateful_events": len(expected), "processed_rows": rows,
                  "ledger_entries": snap["ledger_entries"]},
    ))  # fmt: skip
    assert lost, "the first pull returned nothing"
    assert record.passed, record.reasons


# 5 --------------------------------------------------------------------------------------
def test_consumer_crash(store: ControlPlaneStore, warehouse: Warehouse, tel: Telemetry) -> None:
    fid = "consumer_crash"
    events = [dict(e) for e in sim_events(RUN.crash_customers, RUN.crash_days, seed=RUN.sim_seed)]
    plans = {role: CrashPlan(seed=RUN.fault_seed, before_rate=RUN.crash_rate,
                             after_rate=RUN.crash_rate)
             for role in (OPERATIONAL, WAREHOUSE)}  # fmt: skip
    probe = OpsProbe(_dlq_reader(store))
    probe.tick()

    # Inject 1: seeded crashes before and after side effects; workers restart after each.
    isolated = LocalPipeline(store, warehouse, crash_plans=plans)
    isolated.run(events)
    crashes = {r: isolated.reports[r].crashes for r in (OPERATIONAL, WAREHOUSE)}
    dead_after_isolated = sum(store.dead_letter_counts().values())
    after_isolated = observed_state(store.snapshot()) == expected_state(events, MAX_ATTEMPTS)
    probe.tick()

    # Inject 2: a poison storm. A bad deploy kills both consumers on EVERY delivery of one
    # event type (payment.failed) across 12 fresh customer flows.
    flow: list[dict[str, Any]] = []
    for i in range(POISON_FLOWS):
        flow += [dict(e) for e in customer_lifecycle(f"cust_storm{i}", changes=1, churn=False)
                 + invoice_lifecycle(f"cust_storm{i}", f"inv_storm{i}", fail_first=1)]  # fmt: skip
    poison = {e["event_id"] for e in flow if e["event_type"] == "payment.failed"}
    storm = LocalPipeline(store, warehouse, crash_plans={
        role: CrashPlan(poison=set(poison)) for role in (OPERATIONAL, WAREHOUSE)})  # fmt: skip
    probe.mark()
    storm.run(flow)
    letters = _dead_letters(store)
    per_sub = {sub: sum(1 for s, _ in letters if s == sub) for sub, _ in letters}
    healthy_dead = sum(1 for _, eid in letters if eid not in poison)
    storm_crashes = {r: storm.reports[r].crashes for r in (OPERATIONAL, WAREHOUSE)}
    probe.tick()
    ticks, wall = detection(probe, ("dlq_backlog_high",))

    # Recover: the fix is deployed (no poison crash), then the pubsub-backlog runbook's
    # command (`dlq --redrive` = redrive_all) and a drain.
    fixed = LocalPipeline(store, warehouse)
    report = redrive_all(store, fixed.broker, fixed.topology.events_topic)
    fixed.drain()
    clear = probe.until_clear(("dlq_backlog_high",), max_ticks=5)

    everything = events + flow
    state_ok = observed_state(store.snapshot()) == expected_state(everything, MAX_ATTEMPTS)
    ids_done, rows = _processed(store)
    wh_rows = warehouse.count("raw.sim_events")
    poison_only = (healthy_dead == 0 and len(poison) == POISON_FLOWS
                   and set(per_sub.values()) == {POISON_FLOWS} and len(per_sub) == 2)  # fmt: skip
    record = judge(Measured(
        fid,
        actual=(f"isolated crashes {crashes} over {len(events)} events: dead letters "
                f"{dead_after_isolated}, state == oracle {after_isolated}; poison storm "
                f"({len(poison)} poison events, crashes {storm_crashes}): dead letters per "
                f"subscription {per_sub}, healthy messages dead-lettered {healthy_dead}"),
        detection_ticks=ticks, detection_wall_ms=wall, quiet_fired=quiet_fired(probe, fid),
        recovered=(after_isolated and dead_after_isolated == 0 and sum(crashes.values()) > 0
                   and poison_only and state_ok and store.dead_letter_counts() == {}),
        recovery_actual=(
            f"isolated phase: {dead_after_isolated} dead letters (expected 0); storm: only "
            f"poison dead-lettered {poison_only}; after the fix and the runbook redrive of "
            f"{report.republished}: state == oracle {state_ok}, open dead letters "
            f"{sum(store.dead_letter_counts().values())}"),
        clear_ticks=clear,
        data_loss=len(_stateful(everything) - ids_done)
        + max(0, len({e["event_id"] for e in everything}) - wh_rows),
        duplicate_side_effects=(rows - len(ids_done))
        + max(0, wh_rows - len({e["event_id"] for e in everything})),
        evidence={"crashes": crashes, "storm_crashes": storm_crashes,
                  "isolated_dead_letters": dead_after_isolated,
                  "storm_dead_letters_per_subscription": per_sub,
                  "storm_healthy_dead_letters": healthy_dead, "poison_events": len(poison),
                  "redriven": report.republished, "warehouse_rows": wh_rows,
                  "crash_seed": RUN.fault_seed, "crash_rate": RUN.crash_rate},
    ))  # fmt: skip
    assert record.passed, record.reasons


# 6 --------------------------------------------------------------------------------------
def test_service_restart(pg_url: str, store: ControlPlaneStore, tel: Telemetry) -> None:
    fid = "service_restart"
    dead = [customer_lifecycle(f"cust_restart{i}")[0] for i in range(12)]  # limit: 10
    for e in dead:
        body, _ = encode(e)
        store.record_dead_letter(DeadLetterRecord("praxis-local-events-operational",
            "handler_error", "db down", e["event_id"], e["event_type"], e["trace_id"],
            e["correlation_id"], 5, body))  # fmt: skip

    # The first monitor process opens the incident.
    first = OpsProbe(_dlq_reader(store), store=PostgresIncidentStore(store.engine))
    first.mark()
    first.tick()
    ticks, wall = detection(first, ("dlq_backlog_high",))

    # Restart: a fresh process (new engine, new in-memory state) continues from Postgres.
    engine2 = make_engine(pg_url)
    store2 = ControlPlaneStore(engine2)
    second = OpsProbe(_dlq_reader(store2), store=PostgresIncidentStore(engine2),
                      start=first.now)  # fmt: skip
    second.tick()
    after_restart = PostgresIncidentStore(engine2).live()

    # API restart with webhook rows pending: process 1 accepts, process 2 processes.
    secret = webhook_secret()
    events = [stripe_event("invoice.payment_failed", stripe_invoice(f"in_rs{i}", status="open"),
                           event_id=f"evt_rs{i}") for i in range(5)]  # fmt: skip

    def post(client: TestClient, ev: dict[str, Any]) -> str:
        raw = body_of(ev)
        return str(client.post("/v1/webhooks/stripe", content=raw, headers={
            "Stripe-Signature": signed(raw, secret, int(time.time())),
            "Content-Type": "application/json"}).json()["status"])  # fmt: skip

    engine_a = make_engine(pg_url)
    accepted = [post(TestClient(create_app(Settings(), webhook_receiver=WebhookReceiver(
        PostgresInbox(engine_a), [secret]))), e) for e in events]  # fmt: skip
    engine_a.dispose()  # process 1 is gone
    engine_b = make_engine(pg_url)
    app_b = TestClient(create_app(Settings(), webhook_receiver=WebhookReceiver(
        PostgresInbox(engine_b), [secret])))  # fmt: skip
    redelivered = [post(app_b, e) for e in events]  # Stripe retries while we restarted
    out = Collect()
    source = Source({f"in_rs{i}": snapshot(f"in_rs{i}") for i in range(5)})
    NotificationProcessor(PostgresInbox(engine_b), {"stripe": source}, out, clock=Clock()).drain()

    # Recover: redrive (runbook), the restarted monitor resolves the incident.
    topo = build_topology()
    redrive_all(store2, MemoryBroker(topo), topo.events_topic)
    clear = second.until_clear(("dlq_backlog_high",), max_ticks=5)
    notified = [e for e, _ in first.notifier.sent] + [e for e, _ in second.notifier.sent]
    with engine2.connect() as conn:
        n_incidents = int(conn.execute(text("SELECT count(*) FROM ops_incidents")).scalar_one())
        inbox = {str(k): int(v) for k, v in conn.execute(text(
            "SELECT provider_event_id, deliveries FROM payment_webhook_inbox")).all()}  # fmt: skip
    record = judge(Measured(
        fid,
        actual=(f"after the monitor restart: {len(after_restart)} live incident(s), "
                f"{n_incidents} incident row(s), notifications {notified}; API restart: accepted "
                f"{accepted}, redelivered {redelivered}, processor published {len(out.events)}"),
        detection_ticks=ticks, detection_wall_ms=wall, quiet_fired=quiet_fired(second, fid),
        recovered=(len(after_restart) == 1
                   and [e for e, _ in second.notifier.sent] == ["resolved"]
                   and accepted == ["accepted"] * len(events)
                   and redelivered == ["duplicate"] * len(events)
                   and len(out.events) == len(events)),
        recovery_actual=(f"restarted monitor notified {[e for e, _ in second.notifier.sent]}; "
                         f"inbox processed {len(out.events)} of {len(events)}"),
        clear_ticks=clear, data_loss=len({e["id"] for e in events} - set(inbox)),
        duplicate_side_effects=(n_incidents - 1) + (notified.count("opened") - 1)
        + (len(out.events) - len({e["event_id"] for e in out.events})),
        evidence={"notifications": notified, "inbox_deliveries": inbox,
                  "internal_events": len(out.events), "fetches": source.fetches},
    ))  # fmt: skip
    engine2.dispose()
    engine_b.dispose()
    assert record.passed, record.reasons


# 12 -------------------------------------------------------------------------------------
def test_event_burst(store: ControlPlaneStore, warehouse: Warehouse, tel: Telemetry) -> None:
    fid = "event_burst"
    events = [dict(e) for e in sim_events(RUN.burst_customers, RUN.burst_days, seed=RUN.sim_seed)]
    p = LocalPipeline(store, warehouse,
                      faults=FaultPlan(seed=RUN.fault_seed,
                                       duplicate_rate=RUN.burst_duplicate_rate))  # fmt: skip
    subs = [p.subscription(r) for r in p.roles]
    probe = OpsProbe(_dlq_reader(store))

    def observe() -> dict[str, int]:
        backlog = p.backlog()
        sample_backlog(tel, subs, p.broker.backlog, lambda _s: 0)
        return backlog

    observe()
    probe.tick()
    probe.mark()
    published = p.publish(events)  # the whole world at once, before any consumer runs
    peak = observe()
    probe.tick()
    ticks, wall = detection(probe, ("queue_backlog_high",))

    started = time.perf_counter()
    p.drain()
    drain_s = time.perf_counter() - started
    left = observe()
    clear = probe.until_clear(("queue_backlog_high",), max_ticks=5)

    state_ok = observed_state(store.snapshot()) == expected_state(events, MAX_ATTEMPTS)
    ids, rows = _processed(store)
    unique = {e["event_id"] for e in events}
    wh_rows = warehouse.count("raw.sim_events")
    deliveries = sum(p.reports[r].deliveries for r in p.roles)
    record = judge(Measured(
        fid,
        actual=(f"{published} events published in one burst; peak backlog {peak}; drained in "
                f"{drain_s:.1f} s ({deliveries / drain_s:,.0f} deliveries/s); "
                f"backlog after {left}"),
        detection_ticks=ticks, detection_wall_ms=wall, quiet_fired=quiet_fired(probe, fid),
        recovered=state_ok and sum(left.values()) == 0 and store.dead_letter_counts() == {},
        recovery_actual=f"state == oracle {state_ok}; dead letters {store.dead_letter_counts()}",
        clear_ticks=clear,
        data_loss=len(_stateful(events) - ids) + max(0, len(unique) - wh_rows),
        duplicate_side_effects=(rows - len(ids)) + max(0, wh_rows - len(unique)),
        evidence={"events": len(events), "deliveries": deliveries,
                  "duplicate_rate": RUN.burst_duplicate_rate, "fault_seed": RUN.fault_seed,
                  "drain_s": round(drain_s, 2), "peak_backlog": peak},
    ))  # fmt: skip
    assert record.passed, record.reasons
