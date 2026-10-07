"""Shared ``PaymentGateway`` contract scenarios (required_test.md s13, "Adapter contract").

Each scenario drives a gateway through ``BillingService`` and asserts the *control-plane*
outcome after the provider's notifications went through the real path: inbox -> processor ->
producer -> broker -> operational consumer -> Postgres. The same functions run against the
``SyntheticPaymentGateway`` (every ``make test``) and against the Stripe Sandbox
(``make stripe-verify``). A ``Harness`` hides only how notifications arrive (drained from the
synthetic provider vs delivered by Stripe to the webhook endpoint) and how long to wait.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from praxis.control.store import ControlPlaneStore
from praxis.payments.gateway import ClockControl, PaymentGateway
from praxis.payments.model import OutcomeStatus, PaymentBehaviour
from praxis.payments.service import BillingService
from praxis.payments.synthetic import add_month
from tests.payments.helpers import PLAN, control_state, profile


class ContractGateway(PaymentGateway, ClockControl, Protocol):
    pass


@dataclass
class Harness:
    service: BillingService
    gateway: ContractGateway
    store: ControlPlaneStore
    run_id: str
    start: datetime
    # Process everything that has arrived until ``done`` holds (or fail with ``what``).
    wait_for: Callable[[Callable[[], bool], str], None]

    def customer(self, name: str) -> str:
        return f"cust_p7_{self.run_id}_{name}"

    def state(self, customer_id: str) -> dict[str, Any]:
        return control_state(self.store, customer_id)


def happy_path(h: Harness) -> dict[str, Any]:
    """Subscription with a good card: customer active, first invoice paid on attempt 1."""
    clock = h.gateway.create_test_clock(h.start, f"praxis-{h.run_id}-happy")
    cid = h.customer("happy")
    enrolment = h.service.enrol(profile(cid), PLAN, PaymentBehaviour.SUCCEEDS, test_clock=clock)

    def done() -> bool:
        s = h.state(cid)
        return bool(s["customer"] == "active" and s["ledger"] == (1, PLAN.base_fee_minor))

    h.wait_for(done, "happy path: active customer with one paid invoice")
    state = h.state(cid)
    assert state["subscription"] == "active"
    assert [(i["state"], i["attempts"], i["paid_minor"]) for i in state["invoices"]] == [
        ("paid", 1, PLAN.base_fee_minor)
    ]
    assert state["customer_pending"] == 0 and all(i["pending"] == 0 for i in state["invoices"])
    return {
        "clock": clock,
        "customer_id": cid,
        "subscription": enrolment.subscription.subscription_id,
        "state": state,
    }


def decline_then_recovery(h: Harness) -> dict[str, Any]:
    """Declined first payment -> open invoice, no subscription; new card + retry -> paid."""
    clock = h.gateway.create_test_clock(h.start, f"praxis-{h.run_id}-decline")
    cid = h.customer("decline")
    enrolment = h.service.enrol(profile(cid), PLAN, PaymentBehaviour.CHARGE_FAILS, test_clock=clock)
    invoice_id = enrolment.subscription.latest_invoice_id
    assert invoice_id is not None
    assert enrolment.subscription.status.value == "incomplete"

    def declined() -> bool:
        s = h.state(cid)
        return bool(s["invoices"]) and s["invoices"][0]["failure"] is not None

    h.wait_for(declined, "decline: invoice open with a failed attempt")
    state = h.state(cid)
    assert state["customer"] == "converted" and state["subscription"] is None
    assert [(i["state"], i["attempts"], i["failure"]) for i in state["invoices"]] == [
        ("open", 1, "card_declined")
    ]
    assert state["ledger"] == (0, 0)

    h.service.update_payment_method(
        enrolment.provider_customer_id, PaymentBehaviour.SUCCEEDS, request_id="fix-card-1"
    )
    outcome = h.service.retry_invoice(invoice_id, request_id="recovery-1")
    assert outcome.status is OutcomeStatus.SUCCEEDED

    def recovered() -> bool:
        s = h.state(cid)
        return bool(s["customer"] == "active" and s["ledger"] == (1, PLAN.base_fee_minor))

    h.wait_for(recovered, "recovery: invoice paid on attempt 2, customer active")
    state = h.state(cid)
    assert [(i["state"], i["attempts"]) for i in state["invoices"]] == [("paid", 2)]
    assert state["subscription"] == "active"
    return {"clock": clock, "customer_id": cid, "invoice_id": invoice_id, "state": state}


def renewal_then_cancellation(h: Harness) -> dict[str, Any]:
    """Test clock past the period end: renewal paid; cancel -> churned (voluntary)."""
    clock = h.gateway.create_test_clock(h.start, f"praxis-{h.run_id}-renewal")
    cid = h.customer("renewal")
    enrolment = h.service.enrol(profile(cid), PLAN, PaymentBehaviour.SUCCEEDS, test_clock=clock)
    h.wait_for(lambda: h.state(cid)["ledger"][0] == 1, "renewal: first invoice paid")

    # One calendar month plus two hours: the renewal invoice is created at the period end
    # and collected an hour later.
    h.gateway.advance_test_clock(clock, add_month(h.start) + timedelta(hours=2))
    h.wait_for(lambda: h.state(cid)["ledger"][0] == 2, "renewal: second invoice paid")
    state = h.state(cid)
    assert [(i["state"], i["attempts"]) for i in state["invoices"]] == [("paid", 1), ("paid", 1)]

    h.service.cancel(enrolment.subscription.subscription_id)
    h.service.cancel(enrolment.subscription.subscription_id)  # idempotent

    def churned() -> bool:
        return bool(h.state(cid)["customer"] == "churned")

    h.wait_for(churned, "cancellation: customer churned")
    state = h.state(cid)
    assert state["subscription"] == "cancelled" and state["churn_reason"] == "voluntary"
    assert state["ledger"] == (2, 2 * PLAN.base_fee_minor)
    return {"clock": clock, "customer_id": cid, "state": state}


def idempotent_enrolment(h: Harness) -> dict[str, Any]:
    """Re-running an enrolment (crash/retry) reuses every provider object."""
    clock = h.gateway.create_test_clock(h.start, f"praxis-{h.run_id}-retry")
    cid = h.customer("retry")
    first = h.service.enrol(profile(cid), PLAN, PaymentBehaviour.SUCCEEDS, test_clock=clock)
    again = h.service.enrol(profile(cid), PLAN, PaymentBehaviour.SUCCEEDS, test_clock=clock)
    assert again == first
    h.wait_for(lambda: h.state(cid)["ledger"] == (1, PLAN.base_fee_minor), "retry: one payment")
    state = h.state(cid)
    assert len(state["invoices"]) == 1 and state["customer"] == "active"
    return {"clock": clock, "customer_id": cid, "state": state}


SCENARIOS: dict[str, Callable[[Harness], dict[str, Any]]] = {
    "happy_path": happy_path,
    "decline_then_recovery": decline_then_recovery,
    "renewal_then_cancellation": renewal_then_cancellation,
    "idempotent_enrolment": idempotent_enrolment,
}
