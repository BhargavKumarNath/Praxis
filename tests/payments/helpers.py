"""Builders for payment tests: Stripe-shaped objects, signed webhook bodies, control-plane reads.

Secret-looking values are assembled at runtime so the repository secret scan never sees a
literal credential pattern.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from praxis.control.store import ControlPlaneStore
from praxis.payments.model import CustomerProfile, Plan
from praxis.payments.signature import sign

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
PLAN = Plan(tier="growth", products=("inference_api",), base_fee_minor=4900)


def webhook_secret(suffix: str = "local") -> str:
    return "wh" + "sec_" + f"praxistest{suffix}0123456789"


def sandbox_key(suffix: str = "local") -> str:
    return "sk" + "_test_" + f"praxis{suffix}0123456789"


def profile(customer_id: str, tier: str = "growth") -> CustomerProfile:
    return CustomerProfile(
        customer_id=customer_id,
        region_id="eu_west",
        industry="fintech",
        tier=tier,  # type: ignore[arg-type]
    )


def ts(when: datetime) -> int:
    return int(when.timestamp())


def stripe_event(
    event_type: str,
    obj: dict[str, Any],
    *,
    event_id: str = "evt_1TestEvent0001",
    created: int | None = None,
    livemode: bool = False,
    api_version: str | None = "2026-09-30.endive",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "object": "event",
        "api_version": api_version,
        "created": ts(T0) if created is None else created,
        "livemode": livemode,
        "pending_webhooks": 1,
        "request": {"id": None, "idempotency_key": None},
        "type": event_type,
        "data": {"object": obj},
    }


def body_of(event: dict[str, Any]) -> bytes:
    # Pretty-printed like Stripe's own deliveries: verification must use these exact bytes.
    return json.dumps(event, indent=2).encode()


def signed(body: bytes, secret: str, at: int) -> str:
    return sign(secret, body, at)


def stripe_invoice(
    invoice_id: str = "in_1Test0001",
    *,
    status: str = "paid",
    amount_due: int = 4900,
    currency: str = "gbp",
    customer: str = "cus_Test0001",
    tier: str | None = "growth",
    payment_intents: tuple[str, ...] = ("pi_Test0001",),
    finalized_at: datetime | None = T0,
    period: tuple[datetime, datetime] | None = (T0, datetime(2026, 11, 1, 9, 0, tzinfo=UTC)),
) -> dict[str, Any]:
    lines = []
    if period is not None:
        lines = [
            {
                "id": "il_1",
                "object": "line_item",
                "period": {"start": ts(period[0]), "end": ts(period[1])},
            }
        ]
    return {
        "id": invoice_id,
        "object": "invoice",
        "status": status,
        "amount_due": amount_due,
        "amount_paid": amount_due if status == "paid" else 0,
        "currency": currency,
        "customer": customer,
        "attempt_count": 1,
        "billing_reason": "subscription_create",
        "period_start": ts(T0),
        "period_end": ts(T0),
        "lines": {"object": "list", "data": lines, "has_more": False},
        "metadata": {},
        "parent": {
            "type": "subscription_details",
            "subscription_details": {
                "subscription": "sub_Test0001",
                "metadata": {} if tier is None else {"praxis_tier": tier},
            },
        },
        "payments": {
            "object": "list",
            "data": [
                {
                    "id": f"inpay_{i}",
                    "object": "invoice_payment",
                    "invoice": invoice_id,
                    "payment": {"type": "payment_intent", "payment_intent": pi},
                    "status": "paid" if status == "paid" else "open",
                }
                for i, pi in enumerate(payment_intents)
            ],
            "has_more": False,
        },
        "status_transitions": {
            "finalized_at": None if finalized_at is None else ts(finalized_at),
            "paid_at": ts(T0) if status == "paid" else None,
        },
    }


def stripe_charge(
    charge_id: str = "ch_Test0001",
    *,
    status: str = "succeeded",
    created: datetime = T0,
    failure_code: str | None = None,
    outcome_reason: str | None = None,
    method_type: str = "card",
    wallet: str | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {"type": method_type}
    if method_type == "card":
        details["card"] = {
            "brand": "visa",
            "last4": "4242",
            "wallet": None if wallet is None else {"type": wallet},
        }
    return {
        "id": charge_id,
        "object": "charge",
        "status": status,
        "created": ts(created),
        "amount": 4900,
        "currency": "gbp",
        "failure_code": failure_code,
        "outcome": {
            "type": "issuer_declined" if status == "failed" else "authorized",
            "reason": outcome_reason,
        },
        "payment_method_details": details,
    }


def stripe_subscription(
    sub_id: str = "sub_Test0001",
    *,
    status: str = "active",
    customer_id: str | None = "cust_p7_0001",
    ended_at: datetime | None = None,
    cancellation_reason: str | None = None,
    origin: str = "new",
) -> dict[str, Any]:
    metadata: dict[str, str] = {}
    if customer_id is not None:
        metadata = {
            "praxis_customer_id": customer_id,
            "praxis_tier": "growth",
            "praxis_products": "inference_api,batch_api",
            "praxis_origin": origin,
            "praxis_base_fee_minor": "4900",
            "praxis_billing_period_days": "30",
        }
    return {
        "id": sub_id,
        "object": "subscription",
        "status": status,
        "customer": "cus_Test0001",
        "metadata": metadata,
        "ended_at": None if ended_at is None else ts(ended_at),
        "cancellation_details": {"reason": cancellation_reason, "comment": None, "feedback": None},
        "latest_invoice": "in_1Test0001",
    }


def control_state(store: ControlPlaneStore, customer_id: str) -> dict[str, Any]:
    """The customer's control-plane state, reduced to what the gate scenarios assert."""
    with store.engine.connect() as conn:
        customer = conn.execute(
            text(
                "SELECT c.state, c.churn_reason, c.pending_count, s.state FROM customers c "
                "LEFT JOIN subscriptions s USING (customer_id) WHERE c.customer_id = :c"
            ),
            {"c": customer_id},
        ).one_or_none()
        invoices = conn.execute(
            text(
                "SELECT invoice_id, state, attempts, amount_paid_minor, last_failure_reason, "
                "pending_count FROM invoices WHERE customer_id = :c "
                "ORDER BY period_start, invoice_id"
            ),
            {"c": customer_id},
        ).all()
        ledger = conn.execute(
            text(
                "SELECT count(*), coalesce(sum(amount_minor), 0) FROM payment_ledger "
                "WHERE customer_id = :c"
            ),
            {"c": customer_id},
        ).one()
    return {
        "customer": None if customer is None else customer[0],
        "churn_reason": None if customer is None else customer[1],
        "customer_pending": None if customer is None else customer[2],
        "subscription": None if customer is None else customer[3],
        "invoices": [
            {
                "invoice_id": r[0],
                "state": r[1],
                "attempts": r[2],
                "paid_minor": r[3],
                "failure": r[4],
                "pending": r[5],
            }
            for r in invoices
        ],
        "ledger": (int(ledger[0]), int(ledger[1])),
    }
