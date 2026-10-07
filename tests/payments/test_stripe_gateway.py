"""``StripeGateway`` request shapes and response mapping against Stripe-shaped fixtures.

The HTTP boundary is a route table (``httpx.MockTransport``); business logic is real. Decline
and failure fixtures follow docs.stripe.com/testing (``pm_card_chargeCustomerFail``: generic
decline after attach; decline codes ``insufficient_funds``, ``expired_card``,
``processing_error``) and the 2026-09-30.endive object shapes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from praxis.payments.gateway import ForeignObject, GatewayError, UnsupportedOperation
from praxis.payments.model import (
    ChargeStatus,
    InvoiceStatus,
    OutcomeStatus,
    PaymentBehaviour,
    SubscriptionStatus,
)
from praxis.payments.store import MemoryRefStore
from praxis.payments.stripe_client import StripeApiError, StripeClient
from praxis.payments.stripe_gateway import (
    StripeGateway,
    charge_attempt,
    failure_reason,
    invoice_snapshot,
    subscription_snapshot,
)
from tests.payments.helpers import (
    PLAN,
    T0,
    profile,
    sandbox_key,
    stripe_charge,
    stripe_invoice,
    stripe_subscription,
    ts,
)

Route = Callable[[httpx.Request], httpx.Response] | dict[str, Any]


class FakeStripe:
    """Route table keyed by (method, path); records every request."""

    def __init__(self, routes: dict[tuple[str, str], Route]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(
                404, json={"error": {"type": "invalid_request_error", "code": "resource_missing"}}
            )
        return route(request) if callable(route) else httpx.Response(200, json=route)

    def form(self, i: int) -> dict[str, str]:
        return dict(parse_qsl(self.requests[i].content.decode()))

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


def gateway(fake: FakeStripe, refs: MemoryRefStore | None = None, **kwargs: Any) -> StripeGateway:
    c = StripeClient(
        sandbox_key(), api_version="2026-09-30.endive", transport=httpx.MockTransport(fake)
    )
    return StripeGateway(c, refs or MemoryRefStore(), **kwargs)


# --- pure mapping (fixtures) -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("code", "reason", "expected"),
    [
        ("card_declined", "generic_decline", "card_declined"),
        ("card_declined", "insufficient_funds", "insufficient_funds"),
        ("expired_card", None, "expired_card"),
        ("processing_error", None, "processor_error"),
        ("card_declined", "authentication_required", "authentication_required"),
        ("incorrect_cvc", None, "card_declined"),
        (None, None, "card_declined"),
    ],
)
def test_decline_fixtures_map_to_contract_reasons(
    code: str | None, reason: str | None, expected: str
) -> None:
    assert failure_reason(code, reason) == expected
    failed = charge_attempt(
        stripe_charge(status="failed", failure_code=code, outcome_reason=reason)
    )
    assert (failed.status, failed.failure_reason) == (ChargeStatus.FAILED, expected)


@pytest.mark.parametrize(
    ("method_type", "wallet", "expected"),
    [
        ("card", None, "card"),
        ("card", "apple_pay", "wallet"),
        ("bacs_debit", None, "direct_debit"),
        ("link", None, "wallet"),
    ],
)
def test_payment_method_types(method_type: str, wallet: str | None, expected: str) -> None:
    assert (
        charge_attempt(stripe_charge(method_type=method_type, wallet=wallet)).payment_method
        == expected
    )


def test_succeeded_and_pending_charges_have_no_reason() -> None:
    assert charge_attempt(stripe_charge()).failure_reason is None
    assert charge_attempt(stripe_charge(status="pending")).status is ChargeStatus.PENDING
    with pytest.raises(GatewayError, match="unexpected_charge"):
        charge_attempt({**stripe_charge(), "status": "refunded"})


def test_invoice_fixture_maps_to_snapshot() -> None:
    snap = invoice_snapshot(
        stripe_invoice(status="open"),
        [
            stripe_charge(
                "ch_1",
                status="failed",
                failure_code="card_declined",
                outcome_reason="generic_decline",
            )
        ],
        "cust_a",
    )
    assert (snap.status, snap.amount_minor, snap.currency, snap.tier) == (
        InvoiceStatus.OPEN,
        4900,
        "GBP",
        "growth",
    )
    assert (snap.period_start.isoformat(), snap.period_end.isoformat()) == (
        "2026-10-01",
        "2026-11-01",
    )
    assert snap.finalized_at == T0 and snap.charges[0].failure_reason == "card_declined"


def test_invoice_without_lines_uses_invoice_period_and_metadata_tier() -> None:
    raw = stripe_invoice(period=None, tier=None)
    raw["metadata"] = {"praxis_tier": "starter"}
    raw["parent"] = None
    snap = invoice_snapshot(raw, [], "cust_a")
    assert snap.tier == "starter" and snap.period_start == snap.period_end == T0.date()


def test_invoice_with_unknown_status_or_no_dates_is_rejected() -> None:
    with pytest.raises(GatewayError, match="unexpected_invoice"):
        invoice_snapshot({**stripe_invoice(), "status": "weird"}, [], "cust_a")
    raw = stripe_invoice(period=None)
    raw["period_start"] = None
    with pytest.raises(ForeignObject):
        invoice_snapshot(raw, [], "cust_a")


def test_subscription_fixture_maps_to_snapshot() -> None:
    snap = subscription_snapshot(stripe_subscription(), [T0 + timedelta(days=31), T0])
    assert (snap.customer_id, snap.status, snap.products, snap.activated_at) == (
        "cust_p7_0001",
        SubscriptionStatus.ACTIVE,
        ("inference_api", "batch_api"),
        T0,
    )
    assert snap.cancellation_reason is None


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("cancellation_requested", "voluntary"),
        ("payment_failed", "involuntary_payment"),
        ("payment_disputed", "involuntary_payment"),
        (None, "voluntary"),
    ],
)
def test_cancellation_reasons(reason: str | None, expected: str) -> None:
    raw = stripe_subscription(status="canceled", ended_at=T0, cancellation_reason=reason)
    snap = subscription_snapshot(raw, [T0])
    assert (snap.cancellation_reason, snap.ended_at) == (expected, T0)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(metadata={}),
        lambda s: s["metadata"].update(praxis_tier="platinum"),
        lambda s: s["metadata"].update(praxis_origin="unknown"),
        lambda s: s["metadata"].update(praxis_base_fee_minor="49.00"),
    ],
)
def test_subscriptions_without_praxis_metadata_are_foreign(mutate: Any) -> None:
    raw = stripe_subscription()
    mutate(raw)
    with pytest.raises(ForeignObject):
        subscription_snapshot(raw, [])


def test_unknown_subscription_status_is_rejected() -> None:
    with pytest.raises(GatewayError, match="unexpected_subscription"):
        subscription_snapshot({**stripe_subscription(), "status": "zombie"}, [])


# --- writes --------------------------------------------------------------------------------------
def test_ensure_customer_creates_once_then_reads() -> None:
    customer = {"id": "cus_1", "created": ts(T0), "metadata": {}}
    fake = FakeStripe(
        {("POST", "/v1/customers"): customer, ("GET", "/v1/customers/cus_1"): customer}
    )
    refs = MemoryRefStore()
    gw = gateway(fake, refs)
    first = gw.ensure_customer(profile("cust_a"), test_clock="clock_1")
    again = gw.ensure_customer(profile("cust_a"), test_clock="clock_1")
    assert first == again and first.created_at == T0
    assert fake.paths() == ["POST /v1/customers", "GET /v1/customers/cus_1"]
    form = fake.form(0)
    assert form["test_clock"] == "clock_1" and form["metadata[praxis_customer_id]"] == "cust_a"
    assert "email" not in form and "name" not in form  # no personal data leaves Praxis
    assert fake.requests[0].headers["Idempotency-Key"].startswith("praxis-customer-")
    assert refs.get("stripe", "customer", "cust_a") == "cus_1"


def test_customer_without_created_timestamp_is_rejected() -> None:
    fake = FakeStripe({("POST", "/v1/customers"): {"id": "cus_1"}})
    with pytest.raises(GatewayError, match="unexpected_customer"):
        gateway(fake).ensure_customer(profile("cust_a"))


def test_ensure_plan_creates_product_and_price_once() -> None:
    price = {
        "id": "price_1",
        "unit_amount": 4900,
        "currency": "gbp",
        "recurring": {"interval": "month"},
        "product": "praxis_growth",
    }
    fake = FakeStripe(
        {
            ("POST", "/v1/products"): {"id": "praxis_growth"},
            ("GET", "/v1/prices"): {"data": [], "has_more": False},
            ("POST", "/v1/prices"): price,
        }
    )
    gw = gateway(fake)
    assert gw.ensure_plan(PLAN) == "price_1"
    assert gw.ensure_plan(PLAN) == "price_1"  # from the ref store, no requests
    assert fake.paths() == [
        "GET /v1/products/praxis_growth",
        "POST /v1/products",
        "GET /v1/prices",
        "POST /v1/prices",
    ]
    assert fake.requests[2].url.params["lookup_keys[0]"] == PLAN.lookup_key
    assert fake.form(3) == {
        "product": "praxis_growth",
        "currency": "gbp",
        "unit_amount": "4900",
        "recurring[interval]": "month",
        "lookup_key": PLAN.lookup_key,
        "metadata[praxis_tier]": "growth",
    }


def test_ensure_plan_reuses_a_matching_price_and_refuses_a_different_one() -> None:
    good = {
        "id": "price_9",
        "unit_amount": 4900,
        "currency": "gbp",
        "recurring": {"interval": "month"},
        "product": "praxis_growth",
    }
    fake = FakeStripe(
        {
            ("GET", "/v1/products/praxis_growth"): {"id": "praxis_growth"},
            ("GET", "/v1/prices"): {"data": [good]},
        }
    )
    assert gateway(fake).ensure_plan(PLAN) == "price_9"
    bad = FakeStripe(
        {
            ("GET", "/v1/products/praxis_growth"): {"id": "praxis_growth"},
            ("GET", "/v1/prices"): {"data": [{**good, "unit_amount": 1}]},
        }
    )
    with pytest.raises(GatewayError, match="price_mismatch"):
        gateway(bad).ensure_plan(PLAN)


def test_product_lookup_errors_other_than_404_propagate() -> None:
    fake = FakeStripe(
        {
            ("GET", "/v1/products/praxis_growth"): lambda r: httpx.Response(
                401, json={"error": {"type": "invalid_request_error"}}
            )
        }
    )
    with pytest.raises(StripeApiError):
        gateway(fake).ensure_plan(PLAN)


def test_set_payment_method_attaches_test_card_and_sets_default() -> None:
    fake = FakeStripe(
        {
            ("POST", "/v1/payment_methods/pm_card_chargeCustomerFail/attach"): {"id": "pm_1"},
            ("POST", "/v1/customers/cus_1"): {"id": "cus_1"},
        }
    )
    assert (
        gateway(fake).set_payment_method(
            "cus_1", PaymentBehaviour.CHARGE_FAILS, idempotency_key="praxis-k"
        )
        == "pm_1"
    )
    assert fake.form(0) == {"customer": "cus_1"} and fake.form(1) == {
        "invoice_settings[default_payment_method]": "pm_1"
    }
    assert [r.headers["Idempotency-Key"] for r in fake.requests] == [
        "praxis-k-attach",
        "praxis-k-default",
    ]


@pytest.mark.parametrize(
    "behaviour",
    [
        PaymentBehaviour.INSUFFICIENT_FUNDS,
        PaymentBehaviour.EXPIRED_CARD,
        PaymentBehaviour.PROCESSING_ERROR,
    ],
)
def test_issuer_decline_cards_cannot_be_attached(behaviour: PaymentBehaviour) -> None:
    with pytest.raises(UnsupportedOperation):
        gateway(FakeStripe({})).set_payment_method("cus_1", behaviour, idempotency_key="k")


@pytest.mark.parametrize("latest", ["in_1", {"id": "in_1", "object": "invoice"}, None])
def test_create_subscription_allows_incomplete_and_tags_metadata(latest: Any) -> None:
    fake = FakeStripe(
        {
            ("POST", "/v1/subscriptions"): {
                "id": "sub_1",
                "status": "incomplete",
                "latest_invoice": latest,
            }
        }
    )
    ref = gateway(fake).create_subscription(
        "cust_a", "cus_1", "price_1", PLAN, "new", idempotency_key="praxis-sub"
    )
    assert (ref.subscription_id, ref.status, ref.latest_invoice_id) == (
        "sub_1",
        SubscriptionStatus.INCOMPLETE,
        None if latest is None else "in_1",
    )
    form = fake.form(0)
    assert form["payment_behavior"] == "allow_incomplete" and form["items[0][price]"] == "price_1"
    assert (
        form["metadata[praxis_customer_id]"] == "cust_a"
        and form["metadata[praxis_products]"] == "inference_api"
    )
    assert fake.requests[0].headers["Idempotency-Key"] == "praxis-sub"


def test_pay_invoice_success_and_decline() -> None:
    ok = FakeStripe({("POST", "/v1/invoices/in_1/pay"): {"id": "in_1", "status": "paid"}})
    assert gateway(ok).pay_invoice("in_1", idempotency_key="k").status is OutcomeStatus.SUCCEEDED
    declined = FakeStripe(
        {
            ("POST", "/v1/invoices/in_1/pay"): lambda r: httpx.Response(
                402,
                json={
                    "error": {
                        "type": "card_error",
                        "code": "card_declined",
                        "decline_code": "insufficient_funds",
                    }
                },
            )
        }
    )
    outcome = gateway(declined).pay_invoice("in_1", idempotency_key="k")
    assert (outcome.status, outcome.failure_reason) == (OutcomeStatus.FAILED, "insufficient_funds")


def test_cancel_is_idempotent() -> None:
    active = FakeStripe(
        {
            ("GET", "/v1/subscriptions/sub_1"): {"id": "sub_1", "status": "active"},
            ("DELETE", "/v1/subscriptions/sub_1"): {"id": "sub_1", "status": "canceled"},
        }
    )
    gateway(active).cancel_subscription("sub_1")
    assert active.paths() == ["GET /v1/subscriptions/sub_1", "DELETE /v1/subscriptions/sub_1"]
    done = FakeStripe({("GET", "/v1/subscriptions/sub_1"): {"id": "sub_1", "status": "canceled"}})
    gateway(done).cancel_subscription("sub_1")
    assert done.paths() == ["GET /v1/subscriptions/sub_1"]


# --- reads ---------------------------------------------------------------------------------------
def invoice_routes(
    invoice: dict[str, Any], customer: dict[str, Any] | None = None
) -> dict[tuple[str, str], Route]:
    routes: dict[tuple[str, str], Route] = {
        ("GET", f"/v1/invoices/{invoice['id']}"): invoice,
        ("GET", "/v1/charges"): lambda r: httpx.Response(
            200,
            json={
                "data": {
                    "pi_A": [
                        stripe_charge(
                            "ch_A",
                            status="failed",
                            failure_code="card_declined",
                            outcome_reason="generic_decline",
                        )
                    ],
                    "pi_B": [stripe_charge("ch_B", created=T0 + timedelta(hours=2))],
                }[r.url.params["payment_intent"]],
                "has_more": False,
            },
        ),
    }
    if customer is not None:
        routes[("GET", "/v1/customers/cus_Test0001")] = customer
    return routes


def test_fetch_invoice_collects_charges_across_payment_intents() -> None:
    refs = MemoryRefStore()
    refs.put("stripe", "customer", "cust_a", "cus_Test0001")
    raw = stripe_invoice(payment_intents=("pi_A", "pi_B"))
    raw["payments"]["data"].append({"payment": {"type": "out_of_band_payment"}})
    fake = FakeStripe(invoice_routes(raw))
    snap = gateway(fake, refs).fetch_invoice("in_1Test0001")
    assert snap.customer_id == "cust_a"
    assert [(c.charge_id, c.status) for c in snap.charges] == [
        ("ch_A", ChargeStatus.FAILED),
        ("ch_B", ChargeStatus.SUCCEEDED),
    ]
    assert fake.requests[0].url.params["expand[0]"] == "payments"


def test_fetch_invoice_falls_back_to_customer_metadata() -> None:
    customer = {"id": "cus_Test0001", "metadata": {"praxis_customer_id": "cust_meta"}}
    fake = FakeStripe(invoice_routes(stripe_invoice(payment_intents=()), customer))
    assert gateway(fake).fetch_invoice("in_1Test0001").customer_id == "cust_meta"


@pytest.mark.parametrize(
    "customer", [{"id": "cus_Test0001", "metadata": {}}, {"id": "cus_Test0001", "deleted": True}]
)
def test_invoices_of_foreign_customers_are_foreign(customer: dict[str, Any]) -> None:
    fake = FakeStripe(invoice_routes(stripe_invoice(payment_intents=()), customer))
    with pytest.raises(ForeignObject):
        gateway(fake).fetch_invoice("in_1Test0001")


def test_invoice_without_customer_is_foreign() -> None:
    fake = FakeStripe({("GET", "/v1/invoices/in_x"): {**stripe_invoice("in_x"), "customer": None}})
    with pytest.raises(ForeignObject):
        gateway(fake).fetch_invoice("in_x")


def test_fetch_subscription_activation_from_paid_invoices() -> None:
    paid = [
        {"id": "in_2", "status_transitions": {"paid_at": ts(T0 + timedelta(days=31))}},
        {"id": "in_1", "status_transitions": {"paid_at": ts(T0)}},
        {"id": "in_0", "status_transitions": {"paid_at": None}},
    ]
    fake = FakeStripe(
        {
            ("GET", "/v1/subscriptions/sub_Test0001"): stripe_subscription(),
            ("GET", "/v1/invoices"): {"data": paid, "has_more": False},
        }
    )
    snap = gateway(fake).fetch_subscription("sub_Test0001")
    assert snap.activated_at == T0
    assert (
        fake.requests[1].url.params["subscription"] == "sub_Test0001"
        and fake.requests[1].url.params["status"] == "paid"
    )


# --- Test Clocks --------------------------------------------------------------------------------
def test_test_clock_create_advance_and_delete() -> None:
    target = T0 + timedelta(days=31)
    statuses = iter(
        [
            {"status": "advancing", "frozen_time": ts(T0)},
            {"status": "ready", "frozen_time": ts(target)},
        ]
    )
    fake = FakeStripe(
        {
            ("POST", "/v1/test_helpers/test_clocks"): {"id": "clock_1"},
            ("POST", "/v1/test_helpers/test_clocks/clock_1/advance"): {
                "id": "clock_1",
                "status": "advancing",
            },
            ("GET", "/v1/test_helpers/test_clocks/clock_1"): lambda r: httpx.Response(
                200, json=next(statuses)
            ),
            ("DELETE", "/v1/test_helpers/test_clocks/clock_1"): {"id": "clock_1", "deleted": True},
        }
    )
    sleeps: list[float] = []
    gw = gateway(fake, sleep=sleeps.append, clock_poll_s=1.5)
    assert gw.create_test_clock(T0, "run") == "clock_1"
    gw.advance_test_clock("clock_1", target)
    gw.delete_test_clock("clock_1")
    assert fake.form(0) == {"frozen_time": str(ts(T0)), "name": "run"}
    assert fake.form(1) == {"frozen_time": str(ts(target))}
    assert sleeps == [1.5]


def test_test_clock_failure_and_timeout() -> None:
    failing = FakeStripe(
        {
            ("POST", "/v1/test_helpers/test_clocks/clock_1/advance"): {"id": "clock_1"},
            ("GET", "/v1/test_helpers/test_clocks/clock_1"): {"status": "internal_failure"},
        }
    )
    with pytest.raises(GatewayError, match="test_clock_failure"):
        gateway(failing).advance_test_clock("clock_1", T0)
    stuck = FakeStripe(
        {
            ("POST", "/v1/test_helpers/test_clocks/clock_1/advance"): {"id": "clock_1"},
            ("GET", "/v1/test_helpers/test_clocks/clock_1"): {"status": "advancing"},
        }
    )
    ticks = iter([0.0, 100.0, 200.0])
    gw = gateway(stuck, sleep=lambda s: None, monotonic=lambda: next(ticks), clock_timeout_s=150.0)
    with pytest.raises(GatewayError, match="test_clock_timeout"):
        gw.advance_test_clock("clock_1", T0)


def test_provider_name() -> None:
    assert gateway(FakeStripe({})).provider == "stripe"
