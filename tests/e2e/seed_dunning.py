"""Seed dunning cases for the local console demo through the REAL payment and dunning stack.

The same path as ``tests/dunning/test_e2e.py``: billing on the SyntheticPaymentGateway ->
provider notifications -> inbox -> processor -> event pipeline -> dunning consumer ->
recovery decision (deterministic baseline; no recovery artifact) -> scheduled retry ->
executor charge. Customers enrol over three weeks; a fixed one in three fixes the card
before the next retry, the rest keep failing until the policy closes the case. No randomness:
the outcome pattern is the index, not a draw. SYNTHETIC throughout; no Stripe call.

    python -m tests.e2e.seed_dunning --database-url postgresql+psycopg://...
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine

from praxis.control.store import ControlPlaneStore
from praxis.data.warehouse import Warehouse
from praxis.dunning.consumer import DunningConsumer
from praxis.dunning.executor import Charger, RetryExecutor
from praxis.dunning.tasks import LocalTaskQueue
from praxis.payments.model import PaymentBehaviour
from praxis.payments.processor import NotificationProcessor
from praxis.payments.service import BillingService, deliver_notifications
from praxis.payments.store import PostgresInbox, PostgresRefStore
from praxis.payments.synthetic import SyntheticPaymentGateway
from praxis.streaming.pipeline import LocalPipeline
from praxis.streaming.topology import DUNNING
from tests.dunning.helpers import POLICY, Clock, make_service, rows
from tests.payments.helpers import PLAN, profile

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
DAYS = 21  # enrolment days
TAIL = 30  # days run after the last enrolment so retries play out


class Publisher:
    def __init__(self, pipeline: LocalPipeline) -> None:
        self.pipeline = pipeline

    def publish(self, events: object, /) -> object:
        return self.pipeline.publish(events)  # type: ignore[arg-type]


def seed(database_url: str, per_day: int = 2) -> dict[str, object]:
    engine = create_engine(database_url)
    store = ControlPlaneStore(engine)
    clock, queue = Clock(START), LocalTaskQueue()
    service = make_service(engine, queue=queue, clock=clock)
    warehouse = Warehouse(None)
    warehouse.migrate()
    with tempfile.TemporaryDirectory() as archive:
        pipeline = LocalPipeline(
            store,
            warehouse,
            archive_root=Path(archive),
            extra_consumers={DUNNING: lambda: DunningConsumer(service)},
        )
        gateway = SyntheticPaymentGateway(start=START, refs=PostgresRefStore(engine))
        inbox = PostgresInbox(engine)
        processor = NotificationProcessor(inbox, {gateway.provider: gateway}, Publisher(pipeline))
        billing = BillingService(gateway, Publisher(pipeline))
        executor = RetryExecutor(
            engine, {"synthetic": Charger.for_gateway(gateway)}, POLICY, service, clock=clock
        )

        def settle() -> None:
            deliver_notifications(gateway.drain_notifications(), inbox)
            processor.drain()
            pipeline.drain()

        # One gateway test clock per enrolment day, all advanced daily: provider event times
        # follow the simulated calendar (renewals included), like a Stripe test clock.
        n, clocks = 0, list[str]()
        for day in range(DAYS + TAIL):
            clock.now = START + timedelta(days=day)
            for c in clocks:
                gateway.advance_test_clock(c, clock.now)
            settle()
            if day < DAYS:
                clocks.append(gateway.create_test_clock(clock.now, f"day-{day}"))
                for _ in range(per_day):
                    enrolment = billing.enrol(
                        profile(f"cust_dn_{n:03d}"),
                        PLAN,
                        PaymentBehaviour.CHARGE_FAILS,
                        test_clock=clocks[-1],
                    )
                    settle()
                    if n % 3 == 0:  # this customer fixes the card before the next retry
                        billing.update_payment_method(
                            enrolment.provider_customer_id,
                            PaymentBehaviour.SUCCEEDS,
                            request_id=f"portal-{n}",
                        )
                    n += 1
            queue.run_due(clock.now, executor.handle)
            settle()
    states = rows(engine, "SELECT state, count(*) FROM dunning_cases GROUP BY 1 ORDER BY 1")
    return {"synthetic": True, "customers": n, "cases": dict(states)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args(argv)
    sys.stdout.write(json.dumps(seed(args.database_url), default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
