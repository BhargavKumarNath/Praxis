"""The 11 mandatory Phase 3 event scenarios (required_test.md section 9).

Every scenario runs the production components end to end: producer -> archive ->
in-memory broker (Pub/Sub semantics) -> operational (Postgres), warehouse (DuckDB),
monitoring and DLQ-inspector consumers. Required assertions, checked throughout:

* no duplicate side effect      (processed rows, ledger, warehouse rows)
* final state correct           (control-plane snapshot vs expected)
* DLQ count observable          (``dead_letters`` table + metrics)
* trace ID preserved            (trace / correlation IDs in every sink)
* retries bounded where required (delivery attempts <= max_delivery_attempts)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from praxis.control.db import make_engine
from praxis.control.store import ControlPlaneStore, snapshot_checksum
from praxis.data.warehouse import Warehouse
from praxis.events.codec import encode
from praxis.streaming.faults import CrashPlan, DatabaseOutage
from praxis.streaming.memory import FaultPlan
from praxis.streaming.pipeline import (
    LocalPipeline,
    expected_state,
    observed_state,
    subscription_consistent,
)
from praxis.streaming.producer import replay_archive
from praxis.streaming.redrive import redrive_dead_letters
from praxis.streaming.topology import DLQ_INSPECT, MONITORING, OPERATIONAL, WAREHOUSE
from praxis.tracing import current_correlation_id, current_trace_id
from tests.streaming.helpers import customer_lifecycle, invoice_lifecycle, sim_events

FLOW = customer_lifecycle(changes=1, churn=False) + invoice_lifecycle(fail_first=1)
INV_CREATED, ATT1, FAIL1, ATT2, OK2 = invoice_lifecycle(fail_first=1)
MAX_ATTEMPTS = 5


@pytest.fixture
def warehouse() -> Iterator[Warehouse]:
    w = Warehouse(None)
    w.migrate()
    yield w
    w.close()


@pytest.fixture
def make_pipeline(
    store: ControlPlaneStore, warehouse: Warehouse, tmp_path: Path
) -> Callable[..., LocalPipeline]:
    def make(**kwargs: Any) -> LocalPipeline:
        kwargs.setdefault("archive_root", tmp_path / "archive")
        return LocalPipeline(store, warehouse, **kwargs)

    return make


def _count(store: ControlPlaneStore, sql: str) -> int:
    with store.engine.connect() as conn:
        return int(conn.execute(text(sql)).scalar_one())


def _assert_flow_final(
    p: LocalPipeline, *, events: list[dict[str, Any]] = FLOW, extra_facts: int = 0
) -> None:
    """Final state equals the oracle, and every side effect happened exactly once."""
    snap = p.store.snapshot()
    assert observed_state(snap) == expected_state(events, 3)
    assert subscription_consistent(snap) == []
    assert snap["ledger_entries"] == 1 and snap["ledger_total_minor"] == 12_345
    stateful = sum(1 for e in events if not e["event_type"].startswith(("usage", "request")))
    assert _count(p.store, "SELECT count(*) FROM processed_events") == stateful
    assert _count(p.store, "SELECT count(*) FROM entity_events") == stateful
    assert p.warehouse.count("raw.sim_events") == len({e["event_id"] for e in events}) + extra_facts
    assert sum(p.backlog().values()) == 0


def _assert_traces_preserved(p: LocalPipeline, events: list[dict[str, Any]]) -> None:
    with p.store.engine.connect() as conn:
        ops = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                text("SELECT event_id::text, trace_id, correlation_id FROM processed_events")
            )
        }
    wh = {
        r[0]: (r[1], r[2])
        for r in p.warehouse.con.execute(
            "SELECT event_id, trace_id, correlation_id FROM raw.sim_events"
        ).fetchall()
    }
    for e in events:
        ids = (e["trace_id"], e["correlation_id"])
        assert wh[e["event_id"]] == ids
        if e["event_id"] in ops:
            assert ops[e["event_id"]] == ids


# 1 ----------------------------------------------------------------------------------
def test_scenario_01_normal_event(make_pipeline: Callable[..., LocalPipeline]) -> None:
    p = make_pipeline()
    p.run(FLOW)
    _assert_flow_final(p)
    _assert_traces_preserved(p, FLOW)
    assert p.store.dead_letter_counts() == {}
    assert p.metrics.count(p.subscription(MONITORING), "unique_events_observed") == len(FLOW)
    for role in (OPERATIONAL, WAREHOUSE, MONITORING):
        latency = p.metrics.snapshot()[p.subscription(role)]["latency"]
        assert latency["end_to_end"]["count"] > 0 and latency["processing"]["count"] > 0


# 2 ----------------------------------------------------------------------------------
def test_scenario_02_exact_duplicate(make_pipeline: Callable[..., LocalPipeline]) -> None:
    p = make_pipeline(faults=FaultPlan(seed=2, duplicate_rate=1.0))  # every message twice
    p.run(FLOW)
    p.run(FLOW)  # and the producer publishes the whole flow again
    _assert_flow_final(p)
    ops = p.subscription(OPERATIONAL)
    stateful = _count(p.store, "SELECT count(*) FROM processed_events")
    assert p.metrics.count(ops, "status_applied") == stateful
    assert p.metrics.count(ops, "status_duplicate") == 3 * stateful
    assert p.metrics.count(p.subscription(MONITORING), "duplicate_deliveries") == 3 * len(FLOW)


# 3 ----------------------------------------------------------------------------------
def test_scenario_03_duplicate_after_consumer_restart(
    make_pipeline: Callable[..., LocalPipeline], store: ControlPlaneStore, warehouse: Warehouse
) -> None:
    plan = CrashPlan(after={OK2["event_id"]})  # dies after committing the payment
    p = make_pipeline(crash_plans={OPERATIONAL: plan})
    p.run(FLOW)
    assert p.reports[OPERATIONAL].crashes == 1
    # A completely new process (fresh broker, fresh consumers) receives everything again.
    restarted = LocalPipeline(store, warehouse)
    restarted.run(FLOW)
    _assert_flow_final(restarted)
    assert restarted.metrics.count(restarted.subscription(OPERATIONAL), "status_applied") == 0
    assert warehouse.count("raw.sim_events") == len(FLOW)


# 4 ----------------------------------------------------------------------------------
def test_scenario_04_out_of_order_pair(make_pipeline: Callable[..., LocalPipeline]) -> None:
    p = make_pipeline()
    customer = customer_lifecycle(changes=1, churn=False)
    p.run([OK2, customer[-1]])  # payment result and tier change before their causes
    snap = p.store.snapshot()
    assert snap["invoices"] == {} and snap["customers"] == {} and snap["ledger_entries"] == 0
    assert p.store.pending_summary()["invoice_orphans"] == 1
    p.run([INV_CREATED, ATT2])  # attempt 2 before attempt 1: still not payable
    assert p.store.snapshot()["invoices"]["inv_1"][1] == "open"
    assert p.store.snapshot()["ledger_entries"] == 0
    p.run([FAIL1, ATT1, *reversed(customer[:-1])])
    _assert_flow_final(p)
    assert p.store.pending_summary() == dict.fromkeys(
        ("customer_pending", "invoice_pending", "customer_orphans", "invoice_orphans"), 0
    )


def test_scenario_04b_randomly_reordered_and_delayed_stream(
    make_pipeline: Callable[..., LocalPipeline],
) -> None:
    p = make_pipeline(faults=FaultPlan(seed=4, max_delay_s=3 * 3600, reorder=True))
    p.run(FLOW, chunk=len(FLOW))
    _assert_flow_final(p)


# 5 / 6 ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"payload": {"invoice_id": "inv_1", "amount_minor": "lots"}}, "invalid_payload"),
        ({"schema_version": 2}, "unsupported_schema_version"),
    ],
    ids=["05_malformed_payload", "06_unsupported_schema_version"],
)
def test_scenario_05_06_invalid_messages_go_straight_to_dlq(
    make_pipeline: Callable[..., LocalPipeline], patch: dict[str, Any], reason: str
) -> None:
    p = make_pipeline()
    bad = {**INV_CREATED, "event_id": "0b5c3d0e-0000-4000-8000-00000000bad1", **patch}
    _, attrs = encode(INV_CREATED)
    attrs["event_id"] = bad["event_id"]
    p.publish_raw(json.dumps(bad).encode(), attrs)  # a misbehaving foreign producer
    p.run(FLOW)
    _assert_flow_final(p)  # good traffic unaffected
    # Each of the three consumers that receive it dead-letters it once, at attempt 1.
    assert p.store.dead_letter_counts() == {reason: 3}
    with p.store.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT source_subscription, delivery_attempt, trace_id, correlation_id, "
                "event_id, data IS NOT NULL FROM dead_letters"
            )
        ).all()
    assert {r[0] for r in rows} == {p.subscription(r) for r in (OPERATIONAL, WAREHOUSE, MONITORING)}
    assert all(
        r[1:] == (1, INV_CREATED["trace_id"], INV_CREATED["correlation_id"], bad["event_id"], True)
        for r in rows
    )
    for role in (OPERATIONAL, WAREHOUSE, MONITORING):
        assert p.metrics.count(p.subscription(role), f"dead_lettered_{reason}") == 1
    assert p.metrics.count(p.subscription(DLQ_INSPECT), f"reason_{reason}") == 3
    report = redrive_dead_letters(p.store, p.broker, p.topology.events_topic)
    assert (report.republished, report.skipped_undecodable) == (0, 3)  # invalid stays invalid


# 7 ----------------------------------------------------------------------------------
def test_scenario_07_temporary_database_failure(
    make_pipeline: Callable[..., LocalPipeline], store: ControlPlaneStore
) -> None:
    outage = DatabaseOutage(store.engine)
    outage.fail_next(4)
    p = make_pipeline()
    p.run(FLOW)
    outage.remove()
    _assert_flow_final(p)
    ops = p.subscription(OPERATIONAL)
    assert p.metrics.count(ops, "nack_transient") == 4
    assert p.metrics.count(ops, "redeliveries") == 4
    assert p.store.dead_letter_counts() == {}


def test_scenario_07b_outage_longer_than_retry_budget_dead_letters_then_redrives(
    make_pipeline: Callable[..., LocalPipeline], store: ControlPlaneStore
) -> None:
    """Retries are bounded: an outage outlasting the budget dead-letters instead of looping;
    after the fault is fixed an operator redrive restores the exact final state."""
    p = make_pipeline()
    p.run(FLOW[:-1])
    outage = DatabaseOutage(store.engine)
    outage.fail_next(MAX_ATTEMPTS)  # every operational attempt for the last event fails
    p.run([FLOW[-1]])
    outage.remove()
    assert outage.injected == MAX_ATTEMPTS
    ops = p.subscription(OPERATIONAL)
    assert p.metrics.count(ops, "nack_transient") == MAX_ATTEMPTS
    assert store.dead_letter_counts() == {"max_delivery_attempts_exceeded": 1}
    assert store.snapshot()["ledger_entries"] == 0  # the payment is visible as a dead letter

    report = redrive_dead_letters(store, p.broker, p.topology.events_topic)
    p.drain()
    assert (report.republished, report.skipped_undecodable) == (1, 0)
    _assert_flow_final(p)
    assert store.dead_letter_counts() == {}
    assert redrive_dead_letters(store, p.broker, p.topology.events_topic).republished == 0


def test_redriven_messages_measure_latency_from_the_redrive(
    make_pipeline: Callable[..., LocalPipeline], store: ControlPlaneStore
) -> None:
    """Redrive stamps a fresh send time; without it the end-to-end latency would fall back
    to the broker's publish time (virtual 2026-01-01 here) and report months of lag."""
    p = make_pipeline()
    p.run(FLOW[:-1])
    outage = DatabaseOutage(store.engine)
    outage.fail_next(MAX_ATTEMPTS)
    p.run([FLOW[-1]])
    outage.remove()
    redrive_dead_letters(store, p.broker, p.topology.events_topic)
    p.drain()
    _assert_flow_final(p)
    e2e = p.metrics.snapshot()[p.subscription(OPERATIONAL)]["latency"]["end_to_end"]
    assert e2e["max_ms"] < 60_000


# 8 ----------------------------------------------------------------------------------
CRASH_TARGETS = {INV_CREATED["event_id"], OK2["event_id"], FLOW[0]["event_id"]}


def test_scenario_08_consumer_failure_before_ack(
    make_pipeline: Callable[..., LocalPipeline],
) -> None:
    """The process dies before the side effect: nothing was written, redelivery applies it."""
    p = make_pipeline(
        crash_plans={
            OPERATIONAL: CrashPlan(before=set(CRASH_TARGETS)),
            WAREHOUSE: CrashPlan(before={OK2["event_id"]}),
        }
    )
    p.run(FLOW)
    _assert_flow_final(p)
    assert p.reports[OPERATIONAL].crashes == 3 and p.reports[WAREHOUSE].crashes == 1
    ops = p.subscription(OPERATIONAL)
    assert p.metrics.count(ops, "status_duplicate") == 0  # nothing had been committed
    assert p.store.dead_letter_counts() == {}


# 9 ----------------------------------------------------------------------------------
def test_scenario_09_failure_after_side_effect_before_ack(
    make_pipeline: Callable[..., LocalPipeline],
) -> None:
    """The process dies after committing, before acking: redelivery must be a no-op."""
    p = make_pipeline(
        crash_plans={
            OPERATIONAL: CrashPlan(after=set(CRASH_TARGETS)),
            WAREHOUSE: CrashPlan(after={OK2["event_id"]}),
        }
    )
    p.run(FLOW)
    _assert_flow_final(p)  # ledger once, processed once, warehouse row once
    ops = p.subscription(OPERATIONAL)
    assert p.reports[OPERATIONAL].crashes == 3
    assert p.metrics.count(ops, "status_duplicate") == 3  # exactly the three redeliveries
    assert p.store.dead_letter_counts() == {}


def test_scenario_09b_crash_storm_burns_retries_of_in_flight_messages(
    make_pipeline: Callable[..., LocalPipeline],
) -> None:
    """Finding: a consumer that crashes on every event exhausts the delivery attempts of
    messages leased alongside the crashing one, so innocent messages can reach the DLQ.
    Correctness still holds: nothing is applied twice, the dead letters are visible, and
    a redrive after the fault is fixed restores the exact final state."""
    ids = {e["event_id"] for e in FLOW}
    p = make_pipeline(
        crash_plans={OPERATIONAL: CrashPlan(after=set(ids)), WAREHOUSE: CrashPlan(after=set(ids))}
    )
    p.run(FLOW)
    # Deterministic for this seed-free plan: each crash costs every in-flight message one
    # attempt. Warehouse batches lose all 9 after 5 crashes; operational acks 4, loses 5.
    assert p.store.dead_letter_counts() == {"max_delivery_attempts_exceeded": 14}
    assert p.reports[OPERATIONAL].crashes == p.reports[WAREHOUSE].crashes == MAX_ATTEMPTS
    assert p.store.snapshot()["ledger_entries"] == 0
    report = redrive_dead_letters(p.store, p.broker, p.topology.events_topic)
    assert report.republished == 14
    p.drain()
    _assert_flow_final(p)
    assert p.store.dead_letter_counts() == {}


# 10 ---------------------------------------------------------------------------------
def test_scenario_10_poison_event_to_dlq(make_pipeline: Callable[..., LocalPipeline]) -> None:
    """Schema-valid but unprocessable (BIGINT overflow): retried a bounded number of times."""
    poison = invoice_lifecycle("cust_a", "inv_poison", amount=2**63)[0]
    p = make_pipeline()
    p.run([*FLOW, poison])
    _assert_flow_final(p, events=FLOW, extra_facts=1)  # poison never touched state
    ops = p.subscription(OPERATIONAL)
    assert p.metrics.count(ops, "nack_error") == MAX_ATTEMPTS
    assert p.store.dead_letter_counts() == {"max_delivery_attempts_exceeded": 1}
    with p.store.engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT source_subscription, delivery_attempt, event_id, trace_id FROM dead_letters"
            )
        ).one()
    assert tuple(row) == (ops, MAX_ATTEMPTS, poison["event_id"], poison["trace_id"])
    with p.store.engine.connect() as conn:  # logged nowhere in the control plane
        assert (
            conn.execute(
                text("SELECT count(*) FROM entity_events WHERE aggregate_key = 'inv_poison'")
            ).scalar_one()
            == 0
        )


# 11 ---------------------------------------------------------------------------------
def test_scenario_11_replay_from_archived_events(
    make_pipeline: Callable[..., LocalPipeline],
    pg_url_factory: Callable[[], str],
    tmp_path: Path,
) -> None:
    events = list(sim_events(150, 56))
    p = make_pipeline(
        faults=FaultPlan(seed=11, duplicate_rate=0.1, max_delay_s=600, reorder=True),
        crash_plans={OPERATIONAL: CrashPlan(seed=11, before_rate=0.01, after_rate=0.01)},
    )
    p.run(events, chunk=5_000)
    original = p.store.snapshot()
    assert observed_state(original) == expected_state(events, 3)
    assert p.archive is not None

    # (a) Replay into the same system: everything is a duplicate, nothing changes.
    replay_archive(p.archive, p.producer)
    p.drain()
    assert p.store.snapshot() == original

    # (b) Disaster recovery: empty control plane and warehouse, rebuilt from the archive.
    engine = make_engine(pg_url_factory())
    fresh_store = ControlPlaneStore(engine)
    fresh_wh = Warehouse(None)
    fresh_wh.migrate()
    rebuilt = LocalPipeline(fresh_store, fresh_wh, faults=FaultPlan(seed=99, reorder=True))
    n = replay_archive(p.archive, rebuilt.producer)
    rebuilt.drain()
    assert n == len(events)
    assert snapshot_checksum(fresh_store.snapshot()) == snapshot_checksum(original)
    assert fresh_wh.count("raw.sim_events") == len(events)
    engine.dispose()
    fresh_wh.close()


# Gate: one correlation ID traceable across every service ------------------------------
class _ContextCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.seen: list[tuple[str, Any, str | None, str | None]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.seen.append(
            (
                record.getMessage(),
                getattr(record, "event_id", None),
                current_trace_id(),
                current_correlation_id(),
            )
        )


def test_correlation_id_traces_an_event_across_services(
    make_pipeline: Callable[..., LocalPipeline],
) -> None:
    capture = _ContextCapture()
    logger = logging.getLogger("praxis.streaming")
    old_level = logger.level
    logger.addHandler(capture)
    logger.setLevel(logging.DEBUG)
    try:
        p = make_pipeline()
        p.run(FLOW)
    finally:
        logger.removeHandler(capture)
        logger.setLevel(old_level)
    cid, tid = OK2["correlation_id"], OK2["trace_id"]
    invoice_ids = {e["event_id"] for e in invoice_lifecycle(fail_first=1)}
    # operational service (Postgres)
    assert {r["event_id"] for r in p.store.processed_for_correlation(cid)} == invoice_ids
    # warehouse service (DuckDB)
    rows = p.warehouse.con.execute(
        "SELECT event_id, trace_id FROM raw.sim_events WHERE correlation_id = ?", [cid]
    ).fetchall()
    assert {r[0] for r in rows} == invoice_ids and {r[1] for r in rows} == {tid}
    # monitoring service (structured logs bound to the event's trace context)
    observed = {
        eid for msg, eid, t, c in capture.seen if msg == "event observed" and c == cid and t == tid
    }
    assert observed == invoice_ids
    processed = {eid for msg, eid, t, c in capture.seen if msg == "event processed" and c == cid}
    assert processed == invoice_ids
