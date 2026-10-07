"""Webhook parsing, routing and receipt (unit; the inbox is replaced at its boundary)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from praxis.errors import TransientError
from praxis.payments.model import Notification, NotificationKind
from praxis.payments.signature import SignatureError
from praxis.payments.webhook import (
    ROUTED_EVENT_TYPES,
    ROUTES,
    PayloadError,
    PayloadTooLarge,
    ReceiveStatus,
    WebhookReceiver,
    parse_event,
)
from tests.payments.helpers import (
    body_of,
    signed,
    stripe_event,
    stripe_invoice,
    stripe_subscription,
    webhook_secret,
)

SECRET = webhook_secret()
NOW = 1_790_000_000


class FakeInbox:
    def __init__(self, fail: bool = False) -> None:
        self.rows: dict[tuple[str, str], Notification] = {}
        self.deliveries: dict[tuple[str, str], int] = {}
        self.fail = fail

    def record(
        self, notification: Notification, *, body_sha256: str, trace_id: str, correlation_id: str
    ) -> bool:
        if self.fail:
            raise TransientError("database unavailable")
        assert len(body_sha256) == 64 and len(trace_id) == 32 and correlation_id
        key = (notification.provider, notification.event_id)
        self.deliveries[key] = self.deliveries.get(key, 0) + 1
        if key in self.rows:
            return False
        self.rows[key] = notification
        return True


def receiver(inbox: FakeInbox | None = None, **kwargs: Any) -> WebhookReceiver:
    return WebhookReceiver(inbox or FakeInbox(), [SECRET], clock=lambda: NOW, **kwargs)


def deliver(r: WebhookReceiver, event: dict[str, Any]) -> ReceiveStatus:
    body = body_of(event)
    return r.receive(body, signed(body, SECRET, NOW)).status


# --- routing (current Stripe snapshot event names) ----------------------------------------------
@pytest.mark.parametrize("event_type", ROUTED_EVENT_TYPES)
def test_every_routed_type_parses_to_its_object(event_type: str) -> None:
    kind, object_type = ROUTES[event_type]
    if object_type == "invoice":
        obj: dict[str, Any] = stripe_invoice("in_route")
    elif object_type == "subscription":
        obj = stripe_subscription("sub_route")
    else:
        obj = {"id": "inpay_1", "object": "invoice_payment", "invoice": "in_route"}
    parsed = parse_event(body_of(stripe_event(event_type, obj, event_id="evt_route")))
    assert parsed.kind is kind
    assert parsed.object_id == (
        "sub_route" if kind is NotificationKind.SUBSCRIPTION else "in_route"
    )
    assert parsed.notification().provider == "stripe"


def test_gate_event_families_are_routed() -> None:
    # Phase 7 gate: payment failure, payment success, subscription update / deletion, invoice state.
    for required in (
        "invoice.payment_failed",
        "invoice.paid",
        "invoice.payment_succeeded",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.finalized",
        "invoice.voided",
        "invoice.marked_uncollectible",
    ):
        assert required in ROUTES


def test_unknown_event_type_is_acknowledged_and_not_stored() -> None:
    inbox = FakeInbox()
    status = deliver(receiver(inbox), stripe_event("charge.dispute.created", {"object": "dispute"}))
    assert status is ReceiveStatus.IGNORED
    assert inbox.rows == {}


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"{not json", "malformed_json"),
        (b"\xff\xfe", "malformed_json"),
        (b"[1, 2]", "not_an_event"),
        (json.dumps({"object": "charge"}).encode(), "not_an_event"),
    ],
)
def test_malformed_payloads(body: bytes, reason: str) -> None:
    with pytest.raises(PayloadError) as info:
        parse_event(body)
    assert info.value.reason == reason


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda e: e.update(id="not-an-evt"), "invalid_field"),
        (lambda e: e.update(id="evt_"), "invalid_field"),
        (lambda e: e.update(type=7), "invalid_field"),
        (lambda e: e.update(created="yesterday"), "invalid_field"),
        (lambda e: e.update(created=-1), "invalid_field"),
        (lambda e: e.update(livemode="false"), "invalid_field"),
        (lambda e: e.update(data=None), "missing_object"),
        (lambda e: e["data"].update(object=[]), "missing_object"),
        (lambda e: e["data"]["object"].update(object="charge"), "object_type_mismatch"),
        (lambda e: e["data"]["object"].update(id=None), "invalid_field"),
        (lambda e: e["data"]["object"].update(id="x" * 300), "invalid_field"),
    ],
)
def test_invalid_fields(mutate: Any, reason: str) -> None:
    event = stripe_event("invoice.paid", stripe_invoice())
    mutate(event)
    with pytest.raises(PayloadError) as info:
        parse_event(body_of(event))
    assert info.value.reason == reason


def test_invoice_payment_event_without_invoice_is_invalid() -> None:
    event = stripe_event("invoice_payment.paid", {"id": "inpay_1", "object": "invoice_payment"})
    with pytest.raises(PayloadError, match="invalid_field"):
        parse_event(body_of(event))


def test_api_version_is_optional() -> None:
    parsed = parse_event(body_of(stripe_event("invoice.paid", stripe_invoice(), api_version=None)))
    assert parsed.api_version is None and parsed.notification().api_version is None


# --- receipt -------------------------------------------------------------------------------
def test_accept_then_duplicate() -> None:
    inbox = FakeInbox()
    r = receiver(inbox)
    event = stripe_event(
        "invoice.payment_failed", stripe_invoice(status="open"), event_id="evt_dup1"
    )
    assert deliver(r, event) is ReceiveStatus.ACCEPTED
    assert deliver(r, event) is ReceiveStatus.DUPLICATE
    assert inbox.deliveries[("stripe", "evt_dup1")] == 2
    assert inbox.rows[("stripe", "evt_dup1")].kind is NotificationKind.INVOICE


def test_bad_signature_never_reaches_the_inbox() -> None:
    inbox = FakeInbox()
    body = body_of(stripe_event("invoice.paid", stripe_invoice()))
    with pytest.raises(SignatureError):
        receiver(inbox).receive(body, signed(body, webhook_secret("attacker"), NOW))
    with pytest.raises(SignatureError):
        receiver(inbox).receive(body + b" ", signed(body, SECRET, NOW))
    assert inbox.rows == {} and inbox.deliveries == {}


def test_signature_is_checked_before_parsing() -> None:
    with pytest.raises(SignatureError):
        receiver().receive(b"{not json", None)


def test_livemode_events_are_rejected_unless_allowed() -> None:
    event = stripe_event("invoice.paid", stripe_invoice(), livemode=True)
    with pytest.raises(PayloadError, match="livemode_not_allowed"):
        deliver(receiver(), event)
    assert deliver(receiver(allow_livemode=True), event) is ReceiveStatus.ACCEPTED


def test_oversized_body_is_rejected_before_verification() -> None:
    r = receiver(max_body_bytes=100)
    with pytest.raises(PayloadTooLarge):
        r.receive(b"x" * 101, None)


def test_inbox_outage_propagates_as_transient() -> None:
    with pytest.raises(TransientError):
        deliver(receiver(FakeInbox(fail=True)), stripe_event("invoice.paid", stripe_invoice()))


@pytest.mark.parametrize("secrets", [[], [""]])
def test_receiver_requires_a_secret(secrets: list[str]) -> None:
    with pytest.raises(ValueError, match="secret"):
        WebhookReceiver(FakeInbox(), secrets)
