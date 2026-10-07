"""``StripeGateway``: the ``PaymentGateway`` contract on the Stripe API (sandbox only).

Shapes follow API version ``2026-09-30.endive`` (docs checked 2026-10-07): an invoice's
payments are ``invoice.payments`` (``InvoicePayment`` -> ``payment.payment_intent``); a
subscription invoice's subscription metadata is under ``parent.subscription_details``; the
service period is on the invoice lines. Attempts are the charges of the invoice's
PaymentIntents (``attempt_count`` ignores manual retries, so it is not used).

Praxis metadata (``praxis_*``) is written on every object Praxis creates and is how a
webhook object is mapped back to a Praxis customer; objects without it are ``ForeignObject``.
Only synthetic identifiers are sent to Stripe: no names, emails or addresses.

The mapping functions at the top are pure and unit-tested against Stripe-shaped fixtures.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any, cast, get_args

from praxis.events.payloads import FailureReason, PaymentMethod, Tier
from praxis.payments.gateway import ForeignObject, GatewayError, UnsupportedOperation
from praxis.payments.ids import idempotency_key as make_key
from praxis.payments.model import (
    ChargeAttempt,
    ChargeStatus,
    ChurnReason,
    CustomerProfile,
    InvoiceSnapshot,
    InvoiceStatus,
    Origin,
    OutcomeStatus,
    PaymentBehaviour,
    PaymentOutcome,
    Plan,
    ProviderCustomer,
    SubscriptionRef,
    SubscriptionSnapshot,
    SubscriptionStatus,
)
from praxis.payments.store import RefStore
from praxis.payments.stripe_client import StripeApiError, StripeCardError, StripeClient

PROVIDER = "stripe"
_NOT_FOUND = 404
# Stripe test PaymentMethods (docs.stripe.com/testing). Cards that simulate issuer declines
# cannot be attached to a customer; ``pm_card_chargeCustomerFail`` attaches and then declines.
PAYMENT_METHOD_TOKENS: dict[PaymentBehaviour, str] = {
    PaymentBehaviour.SUCCEEDS: "pm_card_visa",
    PaymentBehaviour.CHARGE_FAILS: "pm_card_chargeCustomerFail",
}
_DEBIT_TYPES = frozenset(
    {"bacs_debit", "sepa_debit", "au_becs_debit", "acss_debit", "us_bank_account"}
)
_TIERS: frozenset[str] = frozenset(get_args(Tier))
_CHARGE_STATUS = {
    "succeeded": ChargeStatus.SUCCEEDED,
    "failed": ChargeStatus.FAILED,
    "pending": ChargeStatus.PENDING,
}


def _ts(value: object) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if isinstance(value, int) else None


def _day(value: object) -> date:
    when = _ts(value)
    if when is None:
        raise ForeignObject("missing timestamp")
    return when.date()


def failure_reason(code: str | None, reason: str | None) -> FailureReason:
    """Map Stripe's error ``code`` and decline code / ``outcome.reason`` to the contract."""
    codes = {code, reason}
    if "insufficient_funds" in codes:
        return "insufficient_funds"
    if "expired_card" in codes:
        return "expired_card"
    if "processing_error" in codes:
        return "processor_error"
    if "authentication_required" in codes:
        return "authentication_required"
    return "card_declined"


def charge_attempt(charge: Mapping[str, Any]) -> ChargeAttempt:
    status = _CHARGE_STATUS.get(str(charge.get("status")))
    created = _ts(charge.get("created"))
    if status is None or created is None:
        raise GatewayError("unexpected_charge", f"charge {charge.get('id')} has no status/created")
    details = charge.get("payment_method_details") or {}
    method_type = details.get("type")
    method: PaymentMethod = "card"
    if method_type in _DEBIT_TYPES:
        method = "direct_debit"
    elif method_type != "card" or (details.get("card") or {}).get("wallet"):
        method = "wallet"
    reason = None
    if status is ChargeStatus.FAILED:
        outcome = charge.get("outcome") or {}
        reason = failure_reason(charge.get("failure_code"), outcome.get("reason"))
    return ChargeAttempt(str(charge["id"]), created, status, method, reason)


def _tier(*candidates: object) -> Tier | None:
    for value in candidates:
        if isinstance(value, str) and value in _TIERS:
            return cast(Tier, value)
    return None


def invoice_snapshot(
    invoice: Mapping[str, Any], charges: Iterable[Mapping[str, Any]], customer_id: str
) -> InvoiceSnapshot:
    try:
        status = InvoiceStatus(str(invoice.get("status")))
    except ValueError:
        raise GatewayError("unexpected_invoice", f"invoice {invoice.get('id')} status") from None
    parent = invoice.get("parent") or {}
    sub_meta = (parent.get("subscription_details") or {}).get("metadata") or {}
    lines = (invoice.get("lines") or {}).get("data") or []
    periods = [line["period"] for line in lines if isinstance(line.get("period"), dict)]
    start = min((p["start"] for p in periods), default=invoice.get("period_start"))
    end = max((p["end"] for p in periods), default=invoice.get("period_end"))
    transitions = invoice.get("status_transitions") or {}
    return InvoiceSnapshot(
        provider=PROVIDER,
        invoice_id=str(invoice["id"]),
        customer_id=customer_id,
        status=status,
        amount_minor=int(invoice.get("amount_due") or 0),
        currency=str(invoice.get("currency") or "").upper(),
        period_start=_day(start),
        period_end=_day(end),
        finalized_at=_ts(transitions.get("finalized_at")),
        tier=_tier(sub_meta.get("praxis_tier"), (invoice.get("metadata") or {}).get("praxis_tier")),
        charges=tuple(charge_attempt(c) for c in charges),
    )


def _int_meta(meta: Mapping[str, Any], key: str) -> int:
    value = meta.get(key)
    if not isinstance(value, str) or not value.isdigit():
        raise ForeignObject(f"subscription metadata {key} missing")
    return int(value)


def subscription_snapshot(
    sub: Mapping[str, Any], paid_at: Iterable[datetime]
) -> SubscriptionSnapshot:
    meta = sub.get("metadata") or {}
    customer_id, tier = meta.get("praxis_customer_id"), _tier(meta.get("praxis_tier"))
    origin, products = meta.get("praxis_origin"), meta.get("praxis_products")
    if not customer_id or tier is None or origin not in ("new", "existing") or not products:
        raise ForeignObject(f"subscription {sub.get('id')} has no Praxis metadata")
    try:
        status = SubscriptionStatus(str(sub.get("status")))
    except ValueError:
        raise GatewayError("unexpected_subscription", f"{sub.get('id')} status") from None
    details = sub.get("cancellation_details") or {}
    reason: ChurnReason | None = None
    if status is SubscriptionStatus.CANCELED:
        failed = details.get("reason") in ("payment_failed", "payment_disputed")
        reason = "involuntary_payment" if failed else "voluntary"
    return SubscriptionSnapshot(
        provider=PROVIDER,
        subscription_id=str(sub["id"]),
        customer_id=str(customer_id),
        status=status,
        tier=tier,
        products=tuple(str(products).split(",")),
        origin=cast(Origin, origin),
        base_fee_minor=_int_meta(meta, "praxis_base_fee_minor"),
        billing_period_days=_int_meta(meta, "praxis_billing_period_days"),
        activated_at=min(paid_at, default=None),
        ended_at=_ts(sub.get("ended_at")),
        cancellation_reason=reason,
    )


class StripeGateway:
    def __init__(
        self,
        client: StripeClient,
        refs: RefStore,
        *,
        clock_poll_s: float = 2.0,
        clock_timeout_s: float = 180.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._refs = refs
        self._poll_s = clock_poll_s
        self._clock_timeout_s = clock_timeout_s
        self._sleep = sleep
        self._monotonic = monotonic

    @property
    def provider(self) -> str:
        return PROVIDER

    # --- writes ---------------------------------------------------------------------------
    def ensure_customer(
        self, profile: CustomerProfile, *, test_clock: str | None = None
    ) -> ProviderCustomer:
        existing = self._refs.get(PROVIDER, "customer", profile.customer_id)
        if existing is not None:
            customer = self._client.get(f"/v1/customers/{existing}")
        else:
            customer = self._client.post(
                "/v1/customers",
                {
                    "description": "Praxis synthetic customer",
                    "metadata": {
                        "praxis_customer_id": profile.customer_id,
                        "praxis_region_id": profile.region_id,
                        "praxis_tier": profile.tier,
                        "praxis_synthetic": "true",
                    },
                    "test_clock": test_clock,
                },
                idempotency_key=make_key("customer", profile.customer_id, test_clock or "-"),
            )
            self._refs.put(PROVIDER, "customer", profile.customer_id, str(customer["id"]))
        created = _ts(customer.get("created"))
        if created is None:
            raise GatewayError("unexpected_customer", "customer has no created timestamp")
        return ProviderCustomer(str(customer["id"]), created)

    def ensure_plan(self, plan: Plan) -> str:
        existing = self._refs.get(PROVIDER, "price", plan.lookup_key)
        if existing is not None:
            return existing
        product_id = self._ensure_product(plan)
        found = self._client.get("/v1/prices", {"lookup_keys": [plan.lookup_key], "limit": 1})
        prices = found.get("data") or []
        if prices:
            price = prices[0]
            recurring = price.get("recurring") or {}
            same = (
                price.get("unit_amount") == plan.base_fee_minor
                and str(price.get("currency")).upper() == plan.currency
                and recurring.get("interval") == plan.interval
                and price.get("product") == product_id
            )
            if not same:
                raise GatewayError("price_mismatch", f"lookup key {plan.lookup_key} differs")
        else:
            price = self._client.post(
                "/v1/prices",
                {
                    "product": product_id,
                    "currency": plan.currency.lower(),
                    "unit_amount": plan.base_fee_minor,
                    "recurring": {"interval": plan.interval},
                    "lookup_key": plan.lookup_key,
                    "metadata": {"praxis_tier": plan.tier},
                },
                idempotency_key=make_key("price", plan.lookup_key),
            )
        self._refs.put(PROVIDER, "price", plan.lookup_key, str(price["id"]))
        return str(price["id"])

    def _ensure_product(self, plan: Plan) -> str:
        try:
            return str(self._client.get(f"/v1/products/{plan.product_key}")["id"])
        except StripeApiError as exc:
            if exc.status != _NOT_FOUND:
                raise
        product = self._client.post(
            "/v1/products",
            {
                "id": plan.product_key,
                "name": f"Praxis {plan.tier}",
                "metadata": {"praxis_tier": plan.tier},
            },
            idempotency_key=make_key("product", plan.product_key),
        )
        return str(product["id"])

    def set_payment_method(
        self, provider_customer_id: str, behaviour: PaymentBehaviour, *, idempotency_key: str
    ) -> str:
        token = PAYMENT_METHOD_TOKENS.get(behaviour)
        if token is None:
            raise UnsupportedOperation(f"Stripe cannot attach a {behaviour.value} payment method")
        method = self._client.post(
            f"/v1/payment_methods/{token}/attach",
            {"customer": provider_customer_id},
            idempotency_key=f"{idempotency_key}-attach",
        )
        self._client.post(
            f"/v1/customers/{provider_customer_id}",
            {"invoice_settings": {"default_payment_method": method["id"]}},
            idempotency_key=f"{idempotency_key}-default",
        )
        return str(method["id"])

    def create_subscription(
        self,
        customer_id: str,
        provider_customer_id: str,
        price_id: str,
        plan: Plan,
        origin: Origin,
        *,
        idempotency_key: str,
    ) -> SubscriptionRef:
        sub = self._client.post(
            "/v1/subscriptions",
            {
                "customer": provider_customer_id,
                "items": [{"price": price_id}],
                "collection_method": "charge_automatically",
                # A declined first payment leaves the subscription ``incomplete`` (HTTP 200)
                # instead of failing the request; recovery pays the invoice later.
                "payment_behavior": "allow_incomplete",
                "metadata": {
                    "praxis_customer_id": customer_id,
                    "praxis_tier": plan.tier,
                    "praxis_products": ",".join(plan.products),
                    "praxis_origin": origin,
                    "praxis_base_fee_minor": str(plan.base_fee_minor),
                    "praxis_billing_period_days": str(plan.billing_period_days),
                },
            },
            idempotency_key=idempotency_key,
        )
        latest = sub.get("latest_invoice")
        latest_id = latest.get("id") if isinstance(latest, dict) else latest
        return SubscriptionRef(
            str(sub["id"]),
            SubscriptionStatus(sub["status"]),
            None if latest_id is None else str(latest_id),
        )

    def pay_invoice(self, invoice_id: str, *, idempotency_key: str) -> PaymentOutcome:
        try:
            self._client.post(f"/v1/invoices/{invoice_id}/pay", {}, idempotency_key=idempotency_key)
        except StripeCardError as exc:
            return PaymentOutcome(OutcomeStatus.FAILED, failure_reason(exc.code, exc.decline_code))
        return PaymentOutcome(OutcomeStatus.SUCCEEDED)

    def cancel_subscription(self, subscription_id: str) -> None:
        sub = self._client.get(f"/v1/subscriptions/{subscription_id}")
        if sub.get("status") == SubscriptionStatus.CANCELED.value:
            return
        self._client.delete(f"/v1/subscriptions/{subscription_id}")

    # --- reads (processor) -----------------------------------------------------------------
    def _praxis_customer(self, provider_customer_id: object) -> str:
        if not isinstance(provider_customer_id, str):
            raise ForeignObject("invoice has no customer")
        known = self._refs.praxis_key_for(PROVIDER, "customer", provider_customer_id)
        if known is not None:
            return known
        customer = self._client.get(f"/v1/customers/{provider_customer_id}")
        praxis_id = (customer.get("metadata") or {}).get("praxis_customer_id")
        if customer.get("deleted") or not praxis_id:
            raise ForeignObject(f"customer {provider_customer_id} was not created by Praxis")
        return str(praxis_id)

    def fetch_invoice(self, invoice_id: str) -> InvoiceSnapshot:
        invoice = self._client.get(f"/v1/invoices/{invoice_id}", {"expand": ["payments"]})
        customer_id = self._praxis_customer(invoice.get("customer"))
        charges: list[dict[str, Any]] = []
        for payment in (invoice.get("payments") or {}).get("data") or []:
            inner = payment.get("payment") or {}
            intent = inner.get("payment_intent")
            if inner.get("type") == "payment_intent" and isinstance(intent, str):
                charges.extend(self._client.list_all("/v1/charges", {"payment_intent": intent}))
        return invoice_snapshot(invoice, charges, customer_id)

    def fetch_subscription(self, subscription_id: str) -> SubscriptionSnapshot:
        sub = self._client.get(f"/v1/subscriptions/{subscription_id}")
        paid = self._client.list_all(
            "/v1/invoices", {"subscription": subscription_id, "status": "paid"}
        )
        paid_at = [
            when
            for inv in paid
            if (when := _ts((inv.get("status_transitions") or {}).get("paid_at"))) is not None
        ]
        return subscription_snapshot(sub, paid_at)

    # --- Test Clocks -----------------------------------------------------------------------
    def create_test_clock(self, frozen_time: datetime, name: str) -> str:
        clock = self._client.post(
            "/v1/test_helpers/test_clocks",
            {"frozen_time": int(frozen_time.timestamp()), "name": name},
            idempotency_key=make_key("test_clock", name, str(int(frozen_time.timestamp()))),
        )
        return str(clock["id"])

    def advance_test_clock(self, clock_id: str, to: datetime) -> None:
        target = int(to.timestamp())
        self._client.post(
            f"/v1/test_helpers/test_clocks/{clock_id}/advance",
            {"frozen_time": target},
            idempotency_key=make_key("advance_clock", clock_id, str(target)),
        )
        deadline = self._monotonic() + self._clock_timeout_s
        while True:
            clock = self._client.get(f"/v1/test_helpers/test_clocks/{clock_id}")
            status = clock.get("status")
            if status == "ready" and clock.get("frozen_time") == target:
                return
            if status == "internal_failure":
                raise GatewayError("test_clock_failure", f"clock {clock_id} failed to advance")
            if self._monotonic() >= deadline:
                raise GatewayError("test_clock_timeout", f"clock {clock_id} still {status}")
            self._sleep(self._poll_s)

    def delete_test_clock(self, clock_id: str) -> None:
        self._client.delete(f"/v1/test_helpers/test_clocks/{clock_id}")
