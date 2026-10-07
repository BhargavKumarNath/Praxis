from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from praxis.control.store import ControlPlaneStore
from praxis.data.warehouse import Warehouse
from praxis.payments.processor import NotificationProcessor
from praxis.payments.service import BillingService, deliver_notifications
from praxis.payments.store import PostgresInbox, PostgresRefStore
from praxis.payments.synthetic import SyntheticPaymentGateway
from praxis.streaming.pipeline import LocalPipeline
from tests.payments.contract import Harness
from tests.payments.helpers import T0


@pytest.fixture
def warehouse() -> Iterator[Warehouse]:
    w = Warehouse(None)
    w.migrate()
    yield w
    w.close()


@pytest.fixture
def pipeline(store: ControlPlaneStore, warehouse: Warehouse, tmp_path: Path) -> LocalPipeline:
    """The real Phase 3 path: producer (validate, archive) -> broker -> consumers -> Postgres."""
    return LocalPipeline(store, warehouse, archive_root=tmp_path / "archive")


@pytest.fixture
def inbox(store: ControlPlaneStore) -> PostgresInbox:
    return PostgresInbox(store.engine)


class _PipelinePublisher:
    """Publishes through the producer; ``drain`` runs the consumers afterwards."""

    def __init__(self, pipeline: LocalPipeline) -> None:
        self.pipeline = pipeline
        self.published = 0

    def publish(self, events: object, /) -> object:
        self.published += self.pipeline.publish(events)  # type: ignore[arg-type]
        return None


@pytest.fixture
def synthetic_harness(
    store: ControlPlaneStore, pipeline: LocalPipeline, inbox: PostgresInbox
) -> Callable[..., Harness]:
    def make(run_id: str = "syn", gateway: SyntheticPaymentGateway | None = None) -> Harness:
        gw = gateway or SyntheticPaymentGateway(start=T0, refs=PostgresRefStore(store.engine))
        publisher = _PipelinePublisher(pipeline)
        processor = NotificationProcessor(inbox, {gw.provider: gw}, publisher)
        service = BillingService(gw, publisher)

        def wait_for(done: Callable[[], bool], what: str) -> None:
            # Synthetic notifications are synchronous: one settle round must be enough.
            deliver_notifications(gw.drain_notifications(), inbox)
            processor.drain()
            pipeline.drain()
            assert done(), f"not reached: {what}"

        return Harness(service, gw, store, run_id, T0, wait_for)

    return make
