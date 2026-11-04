"""Phase 15 failures 3 and 10: database timeout, and Stripe duplicate events.

Real boundary: the signed ``POST /v1/webhooks/stripe`` route of the production app (FastAPI,
middleware, signature verification over the raw body) writing to the Postgres inbox. The DB
timeout is a real Postgres ``statement_timeout`` while another session holds a lock on the
inbox table. No Stripe call is made (CLAUDE.md s5): Stripe's deliveries are re-created here
with the same signing scheme, and the provider read side of the processor is a scripted
snapshot source (the HTTP boundary the StripeGateway would call).
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from praxis.api.app import create_app
from praxis.config import Settings
from praxis.control.db import make_engine
from praxis.payments.processor import NotificationProcessor
from praxis.payments.store import PostgresInbox
from praxis.payments.webhook import WebhookReceiver
from praxis.telemetry import Telemetry
from tests.payments.helpers import body_of, signed, stripe_event, stripe_invoice, webhook_secret
from tests.payments.test_processor import Clock, Collect, Source, snapshot
from tests.resilience.harness import CRITERIA, Measured, OpsProbe, detection, judge, quiet_fired

SECRET = webhook_secret()
URL = "/v1/webhooks/stripe"


def _client(engine: Engine) -> TestClient:
    receiver = WebhookReceiver(PostgresInbox(engine), [SECRET])
    return TestClient(create_app(Settings(), webhook_receiver=receiver))


def _deliver(client: TestClient, event: dict[str, Any]) -> tuple[int, str]:
    """One Stripe delivery: Stripe signs every attempt afresh."""
    raw = body_of(event)
    r = client.post(URL, content=raw, headers={
        "Stripe-Signature": signed(raw, SECRET, int(time.time())),
        "Content-Type": "application/json"})  # fmt: skip
    return r.status_code, r.json().get("status") or r.json().get("error", "")


def _inbox(engine: Engine) -> dict[str, tuple[str, int]]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT provider_event_id, status, deliveries FROM payment_webhook_inbox"))  # fmt: skip
        return {r[0]: (r[1], int(r[2])) for r in rows}


def _events(n: int, prefix: str) -> list[dict[str, Any]]:
    return [
        stripe_event("invoice.payment_failed", stripe_invoice(f"in_{prefix}{i:04d}", status="open"),
                     event_id=f"evt_{prefix}{i:04d}")
        for i in range(n)
    ]  # fmt: skip


def test_db_timeout(pg_url: str, pg_engine: Engine, tel: Telemetry) -> None:
    fid = "db_timeout"
    rules = ("webhook_success_slo_breach", "api_availability_slo_breach")
    app_engine = make_engine(pg_url, statement_timeout_ms=CRITERIA.run.db_statement_timeout_ms)
    client = _client(app_engine)
    probe = OpsProbe()  # this process's telemetry: the API runs in-process via TestClient
    probe.tick()
    events = _events(CRITERIA.run.db_timeout_requests, "dbt")

    # Inject: another session holds the inbox lock; every INSERT hits statement_timeout.
    locker = pg_engine.connect()
    held = locker.begin()
    locker.execute(text("LOCK TABLE payment_webhook_inbox IN ACCESS EXCLUSIVE MODE"))
    probe.mark()
    started = time.perf_counter()
    during = [_deliver(client, e) for e in events]
    lock_s = time.perf_counter() - started
    probe.tick()
    ticks, wall = detection(probe, rules)
    held.rollback()
    locker.close()
    acked_without_write = sum(1 for code, _ in during if 200 <= code < 300)

    # Recover: Stripe redelivers every unacknowledged event.
    after = [_deliver(client, e) for e in events]
    clear = probe.until_clear(rules, max_ticks=5)
    inbox = _inbox(pg_engine)
    ids = {e["id"] for e in events}
    lost = len(ids - set(inbox))
    dup = sum(d - 1 for _, d in inbox.values()) + (len(inbox) - len(ids & set(inbox)))
    app_engine.dispose()

    record = judge(Measured(
        fid,
        actual=(f"{len(during)} deliveries during the lock -> {sorted({c for c, _ in during})} "
                f"({sorted({s for _, s in during})}) in {lock_s:.2f} s; "
                f"{acked_without_write} acknowledged without a durable write"),
        detection_ticks=ticks, detection_wall_ms=wall, quiet_fired=quiet_fired(probe, fid),
        recovered=acked_without_write == 0 and all(s == "accepted" for _, s in after),
        recovery_actual=(
            f"redelivery statuses {sorted({s for _, s in after})}; inbox rows {len(inbox)}"),
        clear_ticks=clear, data_loss=lost, duplicate_side_effects=dup,
        evidence={"statement_timeout_ms": CRITERIA.run.db_statement_timeout_ms,
                  "mean_503_ms": round(lock_s * 1000 / len(during), 1),
                  "webhook_outcomes": {p.labels["outcome"]: p.value
                                       for p in tel.snapshot().select("webhook_events_total")},
                  "incidents_notified": [(e, i.rule_id) for e, i in probe.notifier.sent]},
    ))  # fmt: skip
    assert record.passed, record.reasons


def test_stripe_duplicate(pg_engine: Engine, tel: Telemetry) -> None:
    fid = "stripe_duplicate"
    client = _client(pg_engine)
    inbox = PostgresInbox(pg_engine)
    probe = OpsProbe()
    event = stripe_event("invoice.payment_failed", stripe_invoice("in_dup0001", status="open"),
                         event_id="evt_dup0001")  # fmt: skip
    # out-of-order pair for another invoice: the later event arrives first
    later = stripe_event("invoice.paid", stripe_invoice("in_ooo0001"), event_id="evt_ooo_2",
                         created=1_790_000_100)  # fmt: skip
    earlier = stripe_event("invoice.payment_failed", stripe_invoice("in_ooo0001", status="open"),
                           event_id="evt_ooo_1", created=1_790_000_000)  # fmt: skip
    source = Source({"in_dup0001": snapshot("in_dup0001"), "in_ooo0001": snapshot("in_ooo0001")})
    out = Collect()
    processor = NotificationProcessor(inbox, {"stripe": source}, out, clock=Clock())

    probe.mark()
    statuses = [_deliver(client, event), _deliver(client, event)]
    statuses += [_deliver(client, later), _deliver(client, earlier)]
    first = processor.drain()
    published_once = list(out.events)
    statuses.append(_deliver(client, event))  # Stripe retries an event we already processed
    second = processor.drain()
    for _ in range(18):  # ordinary traffic so both SLOs have enough events to be judged
        _deliver(
            client, stripe_event("invoice.paid", stripe_invoice("in_ok"), event_id=f"evt_ok{_}")
        )
    probe.tick()
    rows = _inbox(pg_engine)
    dup_effects = len(out.events) - len(published_once)
    dup_ids = len(out.events) - len({e["event_id"] for e in out.events})
    duplicates_counted = int(
        sum(p.value for p in tel.snapshot().select("webhook_events_total", outcome="duplicate"))
    )
    record = judge(Measured(
        fid,
        actual=(f"delivery statuses {[s for _, s in statuses]}; inbox deliveries for the event "
                f"{rows['evt_dup0001'][1]}; processor fetched {source.fetches}; "
                f"first drain published {first.events_published}, "
                f"second {second.events_published}"),
        detection_ticks={}, detection_wall_ms={}, quiet_fired=quiet_fired(probe, fid),
        recovered=all(code == 200 for code, _ in statuses) and rows["evt_dup0001"][1] == 3,
        recovery_actual="nothing to recover; duplicates acknowledged 200",
        clear_ticks=None, data_loss=len({"evt_dup0001", "evt_ooo_1", "evt_ooo_2"} - set(rows)),
        duplicate_side_effects=dup_effects + dup_ids + source.fetches.count("in_dup0001") - 1,
        signal_observed=duplicates_counted, signal_expected=2,
        evidence={"inbox": rows, "internal_events": len(out.events),
                  "fetches": source.fetches,
                  "alerts": probe.firing()},
    ))  # fmt: skip
    assert record.passed, record.reasons
