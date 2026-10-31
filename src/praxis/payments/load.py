"""Payment-path load (Phase 14 W4, ADR 0022): SyntheticPaymentGateway only, never Stripe.

N synthetic customers enrol through ``BillingService`` (provider customer, payment method,
subscription, first invoice and its charge). Every provider notification is delivered TWICE
to the durable Postgres inbox (at-least-once webhooks), then the processor publishes the
derived events into the local pipeline (operational consumer -> Postgres).

Checked against the gateway itself: the control plane must hold one invoice per customer,
one ledger entry per successful charge, ledger money equal to the paid invoices, and the
duplicate deliveries must add no inbox row and no money. Events are applied through the
operational consumer's own record mapping (no broker: W2 measures the transport).
"""

from __future__ import annotations

import hashlib
import resource
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine

from praxis.control.store import ControlPlaneStore
from praxis.events.codec import decode, encode
from praxis.observability import LatencySample
from praxis.payments.model import CustomerProfile, InvoiceStatus, PaymentBehaviour, Plan
from praxis.payments.processor import NotificationProcessor, ProcessReport
from praxis.payments.service import BillingService, deliver_notifications
from praxis.payments.store import PostgresInbox, PostgresRefStore
from praxis.payments.synthetic import SyntheticPaymentGateway
from praxis.streaming.consumers.operational import OperationalConsumer, to_record

START = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
PLAN = Plan(tier="growth", products=("inference_api",), base_fee_minor=4900)


@dataclass(frozen=True)
class PaymentLoad:
    customers: int
    seed: int
    fail_share: float = 0.1  # share of customers whose card is declined
    settle_every: int = 500  # enrolments between inbox -> processor -> pipeline settles


class _Publisher:
    """Validates each event through the codec and applies it as the operational consumer."""

    def __init__(self, store: ControlPlaneStore) -> None:
        self.store = store
        self.published = 0

    def publish(self, events: Sequence[Mapping[str, Any]], /) -> object:
        for event in events:
            decoded = decode(*encode(dict(event)))
            self.store.apply_event(OperationalConsumer.name, to_record(decoded))
        self.published += len(events)
        return len(events)


def _fails(seed: int, customer_id: str, share: float) -> bool:
    digest = hashlib.blake2b(f"{seed}:{customer_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest) / 2**64 < share


class _Harness:
    def __init__(self, engine: Engine) -> None:
        self.store = ControlPlaneStore(engine)
        self.publisher = _Publisher(self.store)
        self.gateway = SyntheticPaymentGateway(start=START, refs=PostgresRefStore(engine))
        self.inbox = PostgresInbox(engine)
        self.billing = BillingService(self.gateway, self.publisher)
        self.processor = NotificationProcessor(
            self.inbox, {self.gateway.provider: self.gateway}, self.publisher
        )
        self.report = ProcessReport()
        self.notifications = self.inbox_new = self.inbox_duplicate_new = 0

    def settle(self) -> None:
        batch = self.gateway.drain_notifications()
        self.notifications += len(batch)
        self.inbox_new += deliver_notifications(batch, self.inbox)
        self.inbox_duplicate_new += deliver_notifications(batch, self.inbox)  # redelivery
        self.report.add(self.processor.drain())


def _expected(harness: _Harness, invoices: Sequence[str]) -> tuple[int, int]:
    paid = [harness.gateway.fetch_invoice(i) for i in invoices]
    amounts = [p.amount_minor for p in paid if p.status is InvoiceStatus.PAID]
    return len(amounts), sum(amounts)


def run_payment_load(engine: Engine, load: PaymentLoad) -> dict[str, Any]:
    latency = LatencySample(seed=load.seed)
    invoices: list[str] = []
    failing = 0
    harness = _Harness(engine)
    started = time.perf_counter()
    for i in range(load.customers):
        cid = f"cust_load_{i:07d}"
        fails = _fails(load.seed, cid, load.fail_share)
        failing += fails
        behaviour = PaymentBehaviour.CHARGE_FAILS if fails else PaymentBehaviour.SUCCEEDS
        t0 = time.perf_counter()
        enrolment = harness.billing.enrol(_profile(cid), PLAN, behaviour)
        latency.add((time.perf_counter() - t0) * 1000.0)
        invoices.append(str(enrolment.subscription.latest_invoice_id))
        if (i + 1) % load.settle_every == 0:
            harness.settle()
    harness.settle()
    elapsed = time.perf_counter() - started
    snap = harness.store.snapshot()
    paid_count, paid_total = _expected(harness, invoices)
    return {
        "is_synthetic": True,
        "workload": "W4 SyntheticPaymentGateway enrol + charge -> inbox (x2) -> processor",
        "customers": load.customers,
        "seed": load.seed,
        "fail_share": load.fail_share,
        "customers_failing": failing,
        "elapsed_s": round(elapsed, 3),
        "enrolments_per_s": round(load.customers / elapsed, 1) if elapsed else None,
        "notifications": harness.notifications,
        "notifications_per_s": round(harness.notifications / elapsed, 1) if elapsed else None,
        "enrol_latency": latency.summary(),
        "inbox_rows_new": harness.inbox_new,
        "inbox_rows_from_redelivery": harness.inbox_duplicate_new,
        "processor": vars(harness.report),
        "events_applied": harness.publisher.published,
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "invoices_projected": len(snap["invoices"]),
        "ledger_entries": snap["ledger_entries"],
        "ledger_total_minor": snap["ledger_total_minor"],
        "gateway_paid_invoices": paid_count,
        "gateway_paid_total_minor": paid_total,
        "correct": (
            len(snap["invoices"]) == load.customers
            and (snap["ledger_entries"], snap["ledger_total_minor"]) == (paid_count, paid_total)
            and paid_count == load.customers - failing
            and harness.inbox_duplicate_new == 0
            and harness.report.failed == 0
        ),
    }


def _profile(customer_id: str) -> CustomerProfile:
    return CustomerProfile(
        customer_id=customer_id, region_id="eu_west", industry="fintech", tier="growth"
    )
