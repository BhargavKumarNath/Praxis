"""Live Stripe Sandbox gate (Phase 7). Run only via ``make stripe-verify``.

Real path, end to end: ``StripeGateway`` writes to the Stripe Sandbox; Stripe signs and
sends real webhooks, which the Stripe CLI (Docker, ``stripe listen``) forwards to a real
uvicorn server running ``create_app``; the endpoint verifies the signature over the raw body
and inserts into the Postgres inbox; the processor re-fetches each object from Stripe and
publishes internal events through the Phase 3 pipeline into the control plane.

Volume is tiny by design (CLAUDE.md s5): four customers, each on its own Test Clock (deleted
afterwards, which deletes the customers and subscriptions), one product and one price that
are reused across runs. No load is ever sent to Stripe.

The last test replays the *real* events (fetched back from Stripe, re-signed with the
listener's secret) into a second, empty control plane in reverse order with every event
delivered twice, and requires the same final state: duplicate and out-of-order safety on real
Stripe payloads. A JSON report is written to ``data/stripe/verify-report.json``.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import text

from praxis.api.app import create_app
from praxis.config import Settings
from praxis.control.db import make_engine
from praxis.control.store import ControlPlaneStore
from praxis.data.warehouse import Warehouse
from praxis.payments.normalise import BILLING_SOURCE
from praxis.payments.processor import NotificationProcessor
from praxis.payments.service import BillingService
from praxis.payments.signature import sign
from praxis.payments.store import PostgresInbox, PostgresRefStore
from praxis.payments.stripe_client import StripeClient
from praxis.payments.stripe_gateway import StripeGateway
from praxis.payments.webhook import ROUTED_EVENT_TYPES, WebhookReceiver
from praxis.streaming.archive import EventArchive
from praxis.streaming.pipeline import LocalPipeline
from tests.payments.conftest import _PipelinePublisher
from tests.payments.contract import SCENARIOS, Harness
from tests.payments.helpers import control_state
from tests.pg import _admin, _url_for

pytestmark = pytest.mark.stripe_live

LIVE = os.environ.get("PRAXIS_STRIPE_LIVE") == "1"
SETTINGS = Settings() if LIVE else None  # read at import: the autouse fixture strips PRAXIS_*
CLI_IMAGE = os.environ.get("STRIPE_CLI_IMAGE", "stripe/stripe-cli:v1.53.0")
WAIT_S = float(os.environ.get("PRAXIS_STRIPE_WAIT_S", "180"))
REPORT = Path(__file__).resolve().parents[2] / "data" / "stripe" / "verify-report.json"
RESULTS: dict[str, Any] = {"scenarios": {}}

if not LIVE:
    pytest.skip("live Stripe Sandbox suite: run `make stripe-verify`", allow_module_level=True)


def _key() -> str:
    assert SETTINGS is not None
    if SETTINGS.stripe_secret_key is None:
        pytest.fail(
            "PRAXIS_STRIPE_SECRET_KEY (a sandbox sk_test_ key) is required for stripe-verify"
        )
    return SETTINGS.stripe_secret_key.get_secret_value()


class CountingTransport(httpx.HTTPTransport):
    def __init__(self) -> None:
        super().__init__(retries=0)
        self.requests: dict[str, int] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests[request.method] = self.requests.get(request.method, 0) + 1
        return super().handle_request(request)


class TrackingGateway(StripeGateway):
    clocks: list[str]

    def create_test_clock(self, frozen_time: datetime, name: str) -> str:
        clock = super().create_test_clock(frozen_time, name)
        self.clocks.append(clock)
        return clock


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _cli(*args: str, docker_args: tuple[str, ...] = ()) -> list[str]:
    # The key travels as an environment variable (``-e NAME``), never on a command line.
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "-e",
        "STRIPE_API_KEY",
        *docker_args,
        CLI_IMAGE,
        *args,
    ]


@pytest.fixture(scope="module")
def live(pg_template: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:  # noqa: PLR0915 - one live environment
    key = _key()
    env = {**os.environ, "STRIPE_API_KEY": key}
    secret = subprocess.run(  # noqa: S603 - fixed argv
        _cli("listen", "--print-secret"),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout.strip()
    assert secret.startswith("whsec_"), "stripe listen --print-secret returned no signing secret"

    admin = _admin()
    db = f"praxis_stripe_{uuid.uuid4().hex[:8]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db}" TEMPLATE "{pg_template}"'))
    engine = make_engine(_url_for(db))
    store = ControlPlaneStore(engine)
    warehouse = Warehouse(None)
    warehouse.migrate()
    archive = tmp_path_factory.mktemp("stripe-archive")
    pipeline = LocalPipeline(store, warehouse, archive_root=archive)
    inbox = PostgresInbox(engine)

    port = _free_port()
    app = create_app(Settings(), webhook_receiver=WebhookReceiver(inbox, [secret]))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    name = f"praxis-stripe-listen-{uuid.uuid4().hex[:8]}"
    listener = subprocess.Popen(  # noqa: S603 - fixed argv
        _cli(
            "listen",
            "--forward-to",
            f"http://127.0.0.1:{port}/v1/webhooks/stripe",
            "--events",
            ",".join(ROUTED_EVENT_TYPES),
            docker_args=("--name", name),
        ),
        env=env,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()

    def pump() -> None:
        for raw in listener.stderr or []:
            lines.put(raw)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + 120
    # The "Ready!" line contains the signing secret: match it, never store or print it.
    while "Ready!" not in lines.get(timeout=max(0.1, deadline - time.monotonic())):
        assert time.monotonic() < deadline, "stripe listen did not become ready"

    transport = CountingTransport()
    client = StripeClient(
        key, api_version=SETTINGS.stripe_api_version if SETTINGS else "", transport=transport
    )
    gateway = TrackingGateway(client, PostgresRefStore(engine))
    gateway.clocks = []
    publisher = _PipelinePublisher(pipeline)
    processor = NotificationProcessor(inbox, {"stripe": gateway}, publisher)
    run_id = uuid.uuid4().hex[:8]
    start = datetime.now(UTC).replace(microsecond=0)
    try:
        yield {
            "secret": secret,
            "store": store,
            "engine": engine,
            "inbox": inbox,
            "pipeline": pipeline,
            "processor": processor,
            "gateway": gateway,
            "client": client,
            "transport": transport,
            "publisher": publisher,
            "run_id": run_id,
            "start": start,
            "port": port,
            "archive": archive,
        }
    finally:
        for clock in gateway.clocks:
            try:
                gateway.delete_test_clock(clock)
            except Exception as exc:
                RESULTS.setdefault("cleanup_errors", []).append(f"{clock}: {type(exc).__name__}")
        RESULTS["clocks_deleted"] = len(gateway.clocks)
        RESULTS["stripe_requests"] = transport.requests
        RESULTS["inbox"] = inbox.counts()
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(RESULTS, indent=2, sort_keys=True, default=str) + "\n")
        subprocess.run(["docker", "stop", "-t", "2", name], capture_output=True, check=False)  # noqa: S603, S607
        listener.wait(timeout=30)
        server.should_exit = True
        server_thread.join(timeout=10)
        client.close()
        warehouse.close()
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="module")
def harness(live: dict[str, Any]) -> Harness:
    processor: NotificationProcessor = live["processor"]
    pipeline: LocalPipeline = live["pipeline"]

    def wait_for(done: Callable[[], bool], what: str) -> None:
        deadline = time.monotonic() + WAIT_S
        while True:
            processor.drain()
            pipeline.drain()
            if done():
                return
            if time.monotonic() > deadline:
                pytest.fail(
                    f"not reached within {WAIT_S:.0f} s: {what}; inbox {live['inbox'].counts()}"
                )
            time.sleep(2)  # polling an external system (Stripe -> CLI -> endpoint), bounded above

    service = BillingService(live["gateway"], live["publisher"])
    return Harness(service, live["gateway"], live["store"], live["run_id"], live["start"], wait_for)


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_sandbox_scenario(name: str, harness: Harness) -> None:
    started = time.monotonic()
    result = SCENARIOS[name](harness)
    RESULTS["scenarios"][name] = {
        "seconds": round(time.monotonic() - started, 1),
        "customer_id": result["customer_id"],
        "state": result["state"],
    }


def test_real_events_replayed_reversed_and_duplicated_converge(
    live: dict[str, Any], pg_url_factory: Callable[[], str]
) -> None:
    """Duplicate + out-of-order delivery of the real Stripe events into an empty control plane."""
    assert RESULTS["scenarios"], "run after the scenarios"
    with live["engine"].connect() as conn:
        event_ids = [
            r[0]
            for r in conn.execute(
                text("SELECT provider_event_id FROM payment_webhook_inbox ORDER BY received_at")
            )
        ]
        duplicates_seen = conn.execute(
            text("SELECT count(*) FROM payment_webhook_inbox WHERE deliveries > 1")
        ).scalar_one()
    events = [live["client"].get(f"/v1/events/{eid}") for eid in event_ids]

    engine = make_engine(pg_url_factory())
    store = ControlPlaneStore(engine)
    warehouse = Warehouse(None)
    warehouse.migrate()
    pipeline = LocalPipeline(store, warehouse)
    inbox = PostgresInbox(engine)
    receiver = WebhookReceiver(inbox, [live["secret"]])
    # Praxis-side enrolment facts (customer.created, conversion) never come from Stripe: replay
    # them from the live run's producer archive, exactly as published.
    billing = [
        e for e in EventArchive(live["archive"]).iter_events() if e["source"] == BILLING_SOURCE
    ]
    pipeline.publish(billing)
    client = TestClient(create_app(Settings(), webhook_receiver=receiver))
    statuses: dict[str, int] = {}
    for event in [e for e in reversed(events) for _ in range(2)]:
        body = json.dumps(event).encode()
        response = client.post(
            "/v1/webhooks/stripe",
            content=body,
            headers={"Stripe-Signature": sign(live["secret"], body, int(time.time()))},
        )
        assert response.status_code == 200
        statuses[response.json()["status"]] = statuses.get(response.json()["status"], 0) + 1
    gateway = StripeGateway(live["client"], PostgresRefStore(live["engine"]))
    NotificationProcessor(inbox, {"stripe": gateway}, _PipelinePublisher(pipeline)).drain()
    pipeline.drain()
    for scenario in RESULTS["scenarios"].values():
        replayed = control_state(store, scenario["customer_id"])
        original = control_state(live["store"], scenario["customer_id"])
        assert replayed == original, scenario["customer_id"]
    RESULTS["replay"] = {
        "events": len(events),
        "deliveries": statuses,
        "live_events_with_duplicate_delivery": duplicates_seen,
    }
    assert statuses == {"accepted": len(events), "duplicate": len(events)}
    warehouse.close()
    engine.dispose()
