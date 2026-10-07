"""``POST /v1/webhooks/stripe`` over FastAPI + Postgres (required_test.md s13 "Webhook").

Proves: signature over the raw body, tampered body rejected, duplicate delivery safe, fast
acknowledgement, and that slow work is only *enqueued* (the request makes no provider call
and leaves the inbox row pending for the asynchronous processor).
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from praxis.api.app import build_webhook_receiver, create_app
from praxis.config import Settings
from praxis.control.db import make_engine
from praxis.payments.store import PostgresInbox
from praxis.payments.webhook import WebhookReceiver
from praxis.tracing import CORRELATION_HEADER
from tests.payments.helpers import body_of, signed, stripe_event, stripe_invoice, webhook_secret

pytestmark = pytest.mark.integration
SECRET = webhook_secret()
URL = "/v1/webhooks/stripe"
FAST_ACK_P95_MS = 100.0  # Stripe allows seconds; 100 ms leaves room for CI noise


def app_for(engine: Engine | None, **kwargs: Any) -> TestClient:
    receiver = (
        None if engine is None else WebhookReceiver(PostgresInbox(engine), [SECRET], **kwargs)
    )
    return TestClient(create_app(Settings(), webhook_receiver=receiver))


def post(
    client: TestClient, event: dict[str, Any], secret: str = SECRET, body: bytes | None = None
) -> Any:
    raw = body_of(event)
    header = signed(raw, secret, int(time.time()))
    return client.post(
        URL,
        content=body if body is not None else raw,
        headers={"Stripe-Signature": header, "Content-Type": "application/json"},
    )


def inbox_rows(engine: Engine) -> list[tuple[str, str, int, str]]:
    with engine.connect() as conn:
        return [
            (r[0], r[1], r[2], r[3])
            for r in conn.execute(
                text(
                    "SELECT provider_event_id, status, deliveries, object_id "
                    "FROM payment_webhook_inbox ORDER BY 1"
                )
            )
        ]


def test_valid_event_is_acknowledged_and_left_for_async_processing(pg_engine: Engine) -> None:
    client = app_for(pg_engine)
    response = post(
        client,
        stripe_event("invoice.payment_failed", stripe_invoice(status="open"), event_id="evt_ok1"),
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "event_id": "evt_ok1",
        "event_type": "invoice.payment_failed",
    }
    assert response.headers[CORRELATION_HEADER]
    assert inbox_rows(pg_engine) == [
        ("evt_ok1", "pending", 1, "in_1Test0001")
    ]  # enqueued, not processed


def test_duplicate_delivery_is_acknowledged_once(pg_engine: Engine) -> None:
    client = app_for(pg_engine)
    event = stripe_event("invoice.paid", stripe_invoice(), event_id="evt_dup")
    statuses = [post(client, event).json()["status"] for _ in range(3)]
    assert statuses == ["accepted", "duplicate", "duplicate"]
    assert inbox_rows(pg_engine) == [("evt_dup", "pending", 3, "in_1Test0001")]


def test_out_of_order_events_are_all_stored(pg_engine: Engine) -> None:
    client = app_for(pg_engine)
    later = stripe_event("invoice.paid", stripe_invoice(), event_id="evt_2", created=1_790_000_100)
    earlier = stripe_event(
        "invoice.payment_failed",
        stripe_invoice(status="open"),
        event_id="evt_1",
        created=1_790_000_000,
    )
    assert [post(client, e).json()["status"] for e in (later, earlier)] == ["accepted", "accepted"]
    assert [r[0] for r in inbox_rows(pg_engine)] == ["evt_1", "evt_2"]


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("tampered", "invalid_signature:signature_mismatch"),
        ("wrong_secret", "invalid_signature:signature_mismatch"),
        ("missing_header", "invalid_signature:missing_header"),
        ("reserialised", "invalid_signature:signature_mismatch"),
    ],
)
def test_invalid_signatures_are_rejected_and_never_stored(
    pg_engine: Engine, case: str, reason: str
) -> None:
    client = app_for(pg_engine)
    event = stripe_event("invoice.paid", stripe_invoice())
    raw = body_of(event)
    if case == "tampered":
        response = post(client, event, body=raw.replace(b"4900", b"4901"))
    elif case == "wrong_secret":
        response = post(client, event, secret=webhook_secret("attacker"))
    elif case == "reserialised":
        response = post(client, event, body=raw.replace(b"\n", b""))
    else:
        response = client.post(URL, content=raw)
    assert response.status_code == 400
    assert response.json()["error"] == reason
    assert "4900" not in response.text  # the payload is never echoed
    assert inbox_rows(pg_engine) == []


def test_invalid_payload_and_oversized_body(pg_engine: Engine) -> None:
    client = app_for(pg_engine, max_body_bytes=2_000)
    bad = b'{"object": "charge"}'
    response = client.post(
        URL, content=bad, headers={"Stripe-Signature": signed(bad, SECRET, int(time.time()))}
    )
    assert (response.status_code, response.json()["error"]) == (400, "invalid_payload:not_an_event")
    big = b"{" + b" " * 3_000 + b"}"
    response = client.post(
        URL, content=big, headers={"Stripe-Signature": signed(big, SECRET, int(time.time()))}
    )
    assert (response.status_code, response.json()["error"]) == (413, "payload_too_large")


def test_unrouted_event_types_are_ignored(pg_engine: Engine) -> None:
    response = post(
        app_for(pg_engine), stripe_event("customer.updated", {"id": "cus_1", "object": "customer"})
    )
    assert (response.status_code, response.json()["status"]) == (200, "ignored")
    assert inbox_rows(pg_engine) == []


def test_unconfigured_or_unavailable_inbox_answers_503() -> None:
    event = stripe_event("invoice.paid", stripe_invoice())
    unconfigured = post(app_for(None), event)
    assert (unconfigured.status_code, unconfigured.json()["error"]) == (
        503,
        "webhooks_not_configured",
    )
    dead = make_engine("postgresql+psycopg://praxis@127.0.0.1:1/none", connect_timeout_s=1)
    unavailable = post(app_for(dead), event)
    assert (unavailable.status_code, unavailable.json()["error"]) == (503, "inbox_unavailable")


def test_handler_acknowledges_fast(pg_engine: Engine) -> None:
    """Receipt is one indexed INSERT; no provider call can happen (the receiver has no gateway)."""
    client = app_for(pg_engine)
    for i in range(10):  # warm up connections
        post(client, stripe_event("invoice.paid", stripe_invoice(), event_id=f"evt_warm{i}"))
    latencies = []
    for i in range(200):
        event = stripe_event("invoice.paid", stripe_invoice(f"in_{i}"), event_id=f"evt_fast{i}")
        start = time.perf_counter()
        assert post(client, event).status_code == 200
        latencies.append((time.perf_counter() - start) * 1000)
    p95 = statistics.quantiles(latencies, n=20)[-1]
    assert p95 < FAST_ACK_P95_MS, f"p95 {p95:.1f} ms"
    with pg_engine.connect() as conn:
        pending = conn.execute(
            text("SELECT count(*) FROM payment_webhook_inbox WHERE status = 'pending'")
        ).scalar_one()
    assert pending == 210


def test_receiver_is_built_from_settings_only_when_configured(pg_url: str) -> None:
    assert build_webhook_receiver(Settings()) is None
    assert build_webhook_receiver(Settings(stripe_webhook_secret=SecretStr(SECRET))) is None
    configured = Settings(stripe_webhook_secret=SecretStr(SECRET), database_url=SecretStr(pg_url))
    receiver = build_webhook_receiver(configured)
    assert receiver is not None
    raw = body_of(stripe_event("invoice.paid", stripe_invoice(), event_id="evt_settings"))
    assert receiver.receive(raw, signed(raw, SECRET, int(time.time()))).status.value == "accepted"
