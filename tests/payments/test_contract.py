"""PaymentGateway contract suite on the synthetic provider, end to end through the control plane.

The Stripe half of the suite is ``test_stripe_sandbox.py`` (same scenario functions, live).
Also proves the webhook-side guarantees on the synthetic path: duplicate and out-of-order
notification delivery converge to the same state as clean in-order delivery.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest

from praxis.control.store import ControlPlaneStore, snapshot_checksum
from praxis.payments.model import PaymentBehaviour
from praxis.payments.processor import NotificationProcessor
from praxis.payments.service import BillingService, deliver_notifications
from praxis.payments.store import PostgresInbox, PostgresRefStore
from praxis.payments.synthetic import SyntheticPaymentGateway, add_month
from praxis.streaming.pipeline import LocalPipeline
from tests.payments.conftest import _PipelinePublisher
from tests.payments.contract import SCENARIOS, Harness
from tests.payments.helpers import PLAN, T0, control_state, profile

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_contract_scenario_on_synthetic_gateway(
    name: str, synthetic_harness: Callable[..., Harness]
) -> None:
    result = SCENARIOS[name](synthetic_harness())
    assert result["state"]["customer"] in {"active", "churned"}


def test_all_scenarios_share_one_control_plane(synthetic_harness: Callable[..., Harness]) -> None:
    h = synthetic_harness()
    for scenario in SCENARIOS.values():
        scenario(h)
    pending = h.store.pending_summary()
    assert pending == {
        "customer_pending": 0,
        "invoice_pending": 0,
        "customer_orphans": 0,
        "invoice_orphans": 0,
    }


def _run_world(
    store: ControlPlaneStore,
    pipeline: LocalPipeline,
    deliver: Callable[[list], list],  # type: ignore[type-arg]
) -> str:
    """Three customers through enrol / decline / recovery / renewal / cancel; returns checksum."""
    gw = SyntheticPaymentGateway(start=T0, refs=PostgresRefStore(store.engine))
    inbox = PostgresInbox(store.engine)
    publisher = _PipelinePublisher(pipeline)
    service = BillingService(gw, publisher)
    clock = gw.create_test_clock(T0, "world")
    ok = service.enrol(profile("cust_w_ok"), PLAN, PaymentBehaviour.SUCCEEDS, test_clock=clock)
    bad = service.enrol(
        profile("cust_w_bad"), PLAN, PaymentBehaviour.CHARGE_FAILS, test_clock=clock
    )
    service.enrol(profile("cust_w_late"), PLAN, PaymentBehaviour.SUCCEEDS, test_clock=clock)
    service.update_payment_method(
        bad.provider_customer_id, PaymentBehaviour.SUCCEEDS, request_id="r1"
    )
    assert bad.subscription.latest_invoice_id is not None
    service.retry_invoice(bad.subscription.latest_invoice_id, request_id="r1")
    gw.advance_test_clock(clock, add_month(T0) + timedelta(hours=2))
    service.cancel(ok.subscription.subscription_id)

    notifications = deliver(gw.drain_notifications())
    deliver_notifications(notifications, inbox)
    NotificationProcessor(inbox, {gw.provider: gw}, publisher).drain(limit=3)
    pipeline.drain()
    return snapshot_checksum(store.snapshot())


@pytest.fixture
def fresh_pipeline(
    pg_url_factory: Callable[[], str], tmp_path_factory: pytest.TempPathFactory
) -> Callable[[], tuple[ControlPlaneStore, LocalPipeline]]:
    from praxis.control.db import make_engine
    from praxis.data.warehouse import Warehouse

    def make() -> tuple[ControlPlaneStore, LocalPipeline]:
        store = ControlPlaneStore(make_engine(pg_url_factory()))
        warehouse = Warehouse(None)
        warehouse.migrate()
        return store, LocalPipeline(store, warehouse, archive_root=tmp_path_factory.mktemp("a"))

    return make


def test_duplicate_and_reordered_notifications_converge(
    fresh_pipeline: Callable[[], tuple[ControlPlaneStore, LocalPipeline]],
) -> None:
    clean = _run_world(*fresh_pipeline(), deliver=lambda ns: ns)
    # Every notification three times, newest first: the inbox dedupes ids and the processor
    # re-fetches current state, so delivery order and multiplicity cannot change the result.
    chaotic = _run_world(
        *fresh_pipeline(), deliver=lambda ns: [n for n in reversed(ns) for _ in range(3)]
    )
    assert chaotic == clean


def test_clean_world_reaches_expected_states(
    fresh_pipeline: Callable[[], tuple[ControlPlaneStore, LocalPipeline]],
) -> None:
    store, pipeline = fresh_pipeline()
    _run_world(store, pipeline, deliver=lambda ns: ns)
    ok, bad, late = (control_state(store, f"cust_w_{n}") for n in ("ok", "bad", "late"))
    assert (ok["customer"], ok["subscription"], ok["ledger"][0]) == ("churned", "cancelled", 2)
    assert (bad["customer"], [i["attempts"] for i in bad["invoices"]]) == ("active", [2, 1])
    assert (late["customer"], late["ledger"][0]) == ("active", 2)
