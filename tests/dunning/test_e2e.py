"""End-to-end business journey (required_test.md s3):

payment failure -> synthetic provider notification -> inbox -> processor -> event bus ->
operational + dunning consumers -> state transition -> recovery decision -> retry scheduled
-> task executes the charge -> success notification -> recovered state.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine

from praxis.control.store import ControlPlaneStore
from praxis.data.warehouse import Warehouse
from praxis.dunning.consumer import DunningConsumer, to_dunning_event
from praxis.dunning.executor import Charger, RetryExecutor
from praxis.dunning.tasks import LocalTaskQueue
from praxis.payments.model import PaymentBehaviour
from praxis.payments.processor import NotificationProcessor
from praxis.payments.service import BillingService, deliver_notifications
from praxis.payments.store import PostgresInbox, PostgresRefStore
from praxis.payments.synthetic import SyntheticPaymentGateway
from praxis.streaming.pipeline import LocalPipeline
from praxis.streaming.topology import DUNNING
from tests.dunning.helpers import POLICY, T0, Clock, make_service, rows
from tests.payments.helpers import PLAN, control_state, profile

pytestmark = pytest.mark.integration


class Publisher:
    def __init__(self, pipeline: LocalPipeline) -> None:
        self.pipeline = pipeline

    def publish(self, events: object, /) -> object:
        return self.pipeline.publish(events)  # type: ignore[arg-type]


def test_failed_payment_is_recovered_by_a_scheduled_retry(
    store: ControlPlaneStore, pg_engine: Engine, tmp_path: Path
) -> None:
    clock, queue = Clock(T0), LocalTaskQueue()
    service = make_service(pg_engine, queue=queue, clock=clock)
    warehouse = Warehouse(None)
    warehouse.migrate()
    pipeline = LocalPipeline(
        store,
        warehouse,
        archive_root=tmp_path / "archive",
        extra_consumers={DUNNING: lambda: DunningConsumer(service)},
    )
    gateway = SyntheticPaymentGateway(start=T0, refs=PostgresRefStore(pg_engine))
    inbox = PostgresInbox(pg_engine)
    processor = NotificationProcessor(inbox, {gateway.provider: gateway}, Publisher(pipeline))
    billing = BillingService(gateway, Publisher(pipeline))
    executor = RetryExecutor(
        pg_engine, {"synthetic": Charger.for_gateway(gateway)}, POLICY, service, clock=clock
    )
    notes: list[object] = []

    def settle() -> None:
        batch = gateway.drain_notifications()
        notes.extend(batch)
        deliver_notifications(batch, inbox)
        processor.drain()
        pipeline.drain()

    cid = "cust_e2e"
    enrolment = billing.enrol(profile(cid), PLAN, PaymentBehaviour.CHARGE_FAILS)
    invoice = str(enrolment.subscription.latest_invoice_id)
    settle()

    # failure -> transition -> decision -> retry scheduled
    assert control_state(store, cid)["invoices"][0]["state"] == "open"
    assert rows(pg_engine, "SELECT state, provider FROM dunning_cases") == [("grace", "synthetic")]
    job_id, run_at = rows(pg_engine, "SELECT job_id, run_at FROM retry_jobs")[0]
    assert run_at == T0 + timedelta(days=3) and len(queue.tasks) == 1
    (kind,) = rows(pg_engine, "SELECT policy_kind FROM dunning_decisions")[0]
    assert kind == "baseline"

    # the customer fixes the card; the scheduled retry runs at its time
    billing.update_payment_method(
        enrolment.provider_customer_id, PaymentBehaviour.SUCCEEDS, request_id="portal"
    )
    clock.now = run_at
    assert queue.run_due(clock.now, executor.handle) == 1
    settle()

    state = control_state(store, cid)
    assert state["customer"] == "active" and state["ledger"] == (1, PLAN.base_fee_minor)
    assert rows(pg_engine, "SELECT state FROM dunning_cases") == [("recovered",)]
    assert rows(pg_engine, "SELECT state, attempt_number FROM retry_jobs") == [("succeeded", 2)]
    assert len(gateway.fetch_invoice(invoice).charges) == 2

    # every notification and the task delivered again: no new charge, no new job, same state
    deliver_notifications(notes, inbox)  # type: ignore[arg-type]
    processor.drain()
    pipeline.drain()
    assert queue.redeliver(next(iter(queue.history)), executor.handle)
    assert len(gateway.fetch_invoice(invoice).charges) == 2
    assert len(rows(pg_engine, "SELECT 1 FROM retry_jobs")) == 1
    assert control_state(store, cid) == state
    assert job_id.startswith("job-")


def test_consumer_maps_sources_to_providers(pg_engine: Engine) -> None:
    from praxis.events.codec import DecodedEvent
    from praxis.events.envelope import EventEnvelope

    def decoded(source: str, event_type: str) -> DecodedEvent:
        env = EventEnvelope.model_validate(
            {
                "schema_version": 1,
                "event_id": "6f1c2c1e-6c1b-4a53-9d0e-2a1f6b7c8d90",
                "event_type": event_type,
                "source": source,
                "occurred_at": "2026-10-03T12:00:00Z",
                "published_at": "2026-10-03T12:00:01Z",
                "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
                "correlation_id": "0b1a2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
                "causation_id": None,
                "entity_id": "cust_1",
                "is_synthetic": True,
                "payload": {
                    "invoice_id": "in_1",
                    "attempt_number": 1,
                    "amount_minor": 100,
                    "currency": "GBP",
                    "reason": "card_declined",
                    "final": False,
                },
            }
        )
        return DecodedEvent(env, None)  # type: ignore[arg-type]

    assert to_dunning_event(decoded("stripe", "payment.failed")).provider == "stripe"
    assert to_dunning_event(decoded("synthetic-gateway", "payment.failed")).provider == "synthetic"
    assert to_dunning_event(decoded("simulator", "payment.failed")).provider is None
    consumer = DunningConsumer(make_service(pg_engine))
    assert consumer.handle(decoded("simulator", "usage.observed"), None) == "ignored"  # type: ignore[arg-type]
    delivery = type("D", (), {"delivery_attempt": 2})()
    assert consumer.handle(decoded("stripe", "payment.failed"), delivery) == "applied"
