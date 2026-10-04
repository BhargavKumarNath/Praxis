"""Local event pipeline: producer -> in-memory broker -> four consumers -> stores.

Composes the production components (producer, archive, runtime, consumers, Postgres
store, DuckDB warehouse) over the deterministic ``MemoryBroker`` so a whole run, with
injected delivery faults and crashes, is reproducible. ``expected_state`` is the
independent oracle: the Phase 1 ``StreamValidator`` fed the events in their original
order, exactly once.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from praxis.control.store import ControlPlaneStore
from praxis.data.warehouse import Warehouse
from praxis.domain.states import InvoiceState, SubscriptionState
from praxis.simulator.validation import StreamValidator
from praxis.streaming.archive import EventArchive
from praxis.streaming.consumers.dead_letter import DeadLetterWorker
from praxis.streaming.consumers.monitoring import MonitoringConsumer
from praxis.streaming.consumers.operational import OperationalConsumer
from praxis.streaming.consumers.warehouse import WarehouseConsumer
from praxis.streaming.faults import CrashPlan, with_crashes
from praxis.streaming.memory import DrainReport, FaultPlan, MemoryBroker, drain
from praxis.streaming.metrics import StreamMetrics
from praxis.streaming.producer import EventProducer
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

ROLES = (OPERATIONAL, WAREHOUSE, MONITORING)


@dataclass
class LocalPipeline:
    store: ControlPlaneStore
    warehouse: Warehouse
    archive_root: Path | None = None
    topology: Topology = field(default_factory=build_topology)
    faults: FaultPlan = field(default_factory=FaultPlan)
    crash_plans: Mapping[str, CrashPlan] = field(default_factory=dict)
    batch_size: int = 100
    metrics: StreamMetrics = field(default_factory=StreamMetrics)

    def __post_init__(self) -> None:
        self.broker = MemoryBroker(self.topology, faults=self.faults)
        self.archive = EventArchive(self.archive_root) if self.archive_root else None
        self.producer = EventProducer(self.broker, self.topology.events_topic, self.archive)
        self.dead_letters = DeadLetterRouter(self.broker, self.topology.dead_letter_topic)
        self._workers: dict[str, Worker] = {}
        self.reports: dict[str, DrainReport] = {}
        # The monitoring consumer's state is in-process; a restart loses it (by design).
        self._consumers: dict[str, Callable[[], Consumer]] = {
            OPERATIONAL: lambda: OperationalConsumer(self.store),
            WAREHOUSE: lambda: WarehouseConsumer(self.warehouse, "stream"),
            MONITORING: lambda: MonitoringConsumer(self.metrics),
        }

    def _build(self, role: str) -> Worker:
        if role == DLQ_INSPECT:
            return DeadLetterWorker(self.store, self.metrics)
        consumer = self._consumers[role]()
        plan = self.crash_plans.get(role)
        if plan is not None:
            consumer = with_crashes(consumer, plan)
        return ConsumerWorker(consumer, self.dead_letters, self.metrics)

    def worker_factory(self, role: str) -> Callable[[], Worker]:
        """Reuse the running worker; build a new one only after a crash (restart)."""
        resume = True

        def get() -> Worker:
            nonlocal resume
            if resume and role in self._workers:
                resume = False
                return self._workers[role]
            resume = False
            self._workers[role] = self._build(role)
            return self._workers[role]

        return get

    def subscription(self, role: str) -> str:
        return self.topology.by_role(role).name

    def publish(self, events: Sequence[Mapping[str, Any]], *, replay: bool = False) -> int:
        return self.producer.publish(events, replay=replay).published

    def publish_raw(self, data: bytes, attributes: Mapping[str, str]) -> str:
        """Bypass the producer (simulates a misbehaving foreign publisher)."""
        return self.broker.publish(self.topology.events_topic, data, attributes)

    def drain(self) -> dict[str, DrainReport]:
        """Drain every consumer, then the DLQ inspector (which sees all dead letters)."""
        for role in (*ROLES, DLQ_INSPECT):
            report = drain(
                self.broker,
                self.subscription(role),
                self.worker_factory(role),
                batch_size=self.batch_size,
            )
            total = self.reports.setdefault(role, DrainReport())
            for name in ("deliveries", "acks", "nacks", "crashes", "rounds"):
                setattr(total, name, getattr(total, name) + getattr(report, name))
        return self.reports

    def run(self, events: Iterable[Mapping[str, Any]], *, chunk: int = 1000) -> int:
        """Streaming mode: publish a chunk, drain, repeat (latency = send -> processed)."""
        total = 0
        buf: list[Mapping[str, Any]] = []
        for event in events:
            buf.append(event)
            if len(buf) >= chunk:
                total += self.publish(buf)
                self.drain()
                buf = []
        if buf:
            total += self.publish(buf)
            self.drain()
        return total

    def backlog(self) -> dict[str, int]:
        return {
            role: self.broker.backlog(self.subscription(role)) for role in (*ROLES, DLQ_INSPECT)
        }


def expected_state(events: Iterable[Mapping[str, Any]], max_attempts: int) -> dict[str, Any]:
    """Oracle: final business state from feeding events once, in time order, to the validator."""
    validator = StreamValidator(max_attempts, schema_every=1_000_000)
    for event in sorted(events, key=lambda e: str(e["occurred_at"])):  # stable sort
        validator.feed(dict(event))
    customers = validator.customer_states()
    invoices = validator.invoice_states()
    paid = [amt for _, amt, st, _ in invoices.values() if st is InvoiceState.PAID]
    return {
        "customers": {k: [s.value, tier] for k, (s, tier) in sorted(customers.items())},
        "invoices": {
            k: [cust, st.value, amt, att, amt if st is InvoiceState.PAID else 0]
            for k, (cust, amt, st, att) in sorted(invoices.items())
        },
        "ledger_entries": len(paid),
        "ledger_total_minor": sum(paid),
    }


def observed_state(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce ``ControlPlaneStore.snapshot()`` to the oracle's shape."""
    return {
        "customers": {k: [v[0], v[1]] for k, v in snapshot["customers"].items()},
        "invoices": {k: [v[0], v[1], v[2], v[3], v[4]] for k, v in snapshot["invoices"].items()},
        "ledger_entries": snapshot["ledger_entries"],
        "ledger_total_minor": snapshot["ledger_total_minor"],
    }


# Subscription state implied by each customer state (None: no subscription yet / ever).
_IMPLIED_SUBSCRIPTION: dict[str, str | None] = {
    "prospect": None,
    "converted": None,
    "lost": None,
    "active": SubscriptionState.ACTIVE.value,
    "churned": SubscriptionState.CANCELLED.value,
}


def subscription_consistent(snapshot: Mapping[str, Any]) -> list[str]:
    """Customers whose subscription state disagrees with their lifecycle state."""
    return [
        cid for cid, row in snapshot["customers"].items() if _IMPLIED_SUBSCRIPTION[row[0]] != row[5]
    ]
