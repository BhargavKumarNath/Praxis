"""``python -m praxis.payments`` with the provider and bus replaced at their factories."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from praxis.config import Settings
from praxis.payments.__main__ import main, stripe_source
from praxis.payments.gateway import SnapshotSource
from praxis.payments.model import PaymentBehaviour
from praxis.payments.processor import EventPublisher
from praxis.payments.service import BillingService, deliver_notifications
from praxis.payments.store import MemoryRefStore, PostgresInbox, RefStore
from praxis.payments.stripe_gateway import StripeGateway
from praxis.payments.synthetic import SyntheticPaymentGateway
from praxis.streaming.pipeline import LocalPipeline
from tests.payments.conftest import _PipelinePublisher
from tests.payments.helpers import PLAN, T0, profile, sandbox_key

pytestmark = pytest.mark.integration


def run(capsys: pytest.CaptureFixture[str], *argv: str, **factories: object) -> tuple[int, object]:
    code = main(list(argv), **factories)  # type: ignore[arg-type]
    out = capsys.readouterr().out
    # Structured logs are single JSON lines on stdout; the report is the indented last document.
    start = out.rfind("{\n") if "{\n" in out else out.rfind("{")
    return code, json.loads(out[start:])


def test_process_inbox_and_requeue(
    pg_url: str, pipeline: LocalPipeline, capsys: pytest.CaptureFixture[str]
) -> None:
    gw = SyntheticPaymentGateway(start=T0)
    BillingService(gw, _PipelinePublisher(pipeline)).enrol(
        profile("cust_cli"), PLAN, PaymentBehaviour.SUCCEEDS
    )
    inbox = PostgresInbox(pipeline.store.engine)
    delivered = deliver_notifications(gw.drain_notifications(), inbox)

    def source(settings: Settings, refs: RefStore) -> SnapshotSource:
        return gw

    def publisher(args: argparse.Namespace) -> EventPublisher:
        assert args.project == "praxis-local"
        return _PipelinePublisher(pipeline)

    assert run(capsys, "--database-url", pg_url, "inbox") == (0, {"pending": delivered})
    code, report = run(
        capsys,
        "--database-url",
        pg_url,
        "process",
        gateway_factory=source,
        publisher_factory=publisher,
    )
    assert code == 0 and report["processed"] == delivered and report["failed"] == 0  # type: ignore[index]
    with pipeline.store.engine.begin() as conn:  # simulate a row that failed before a fix
        from sqlalchemy import text

        conn.execute(
            text(
                "UPDATE payment_webhook_inbox SET status = 'failed' "
                "WHERE provider_event_id = 'syn_evt_000001'"
            )
        )
    assert run(capsys, "--database-url", pg_url, "requeue") == (0, {"requeued": 1})


def test_process_exit_code_reports_failures(
    pg_url: str, pipeline: LocalPipeline, capsys: pytest.CaptureFixture[str]
) -> None:
    from praxis.payments.model import Notification, NotificationKind

    inbox = PostgresInbox(pipeline.store.engine)
    deliver_notifications(
        [
            Notification(
                "synthetic", "evt_x", "invoice.updated", NotificationKind.INVOICE, "in_x", T0
            )
        ],
        inbox,
    )

    class Broken:
        provider = "synthetic"

        def fetch_invoice(self, invoice_id: str) -> object:
            from praxis.errors import PermanentError

            raise PermanentError("broken", invoice_id)

        fetch_subscription = fetch_invoice

    code, report = run(
        capsys,
        "--database-url",
        pg_url,
        "process",
        gateway_factory=lambda s, r: Broken(),
        publisher_factory=lambda a: _PipelinePublisher(pipeline),
    )
    assert code == 1 and report["failed"] == 1  # type: ignore[index]


def test_database_url_is_required() -> None:
    with pytest.raises(SystemExit, match="database URL"):
        main(["inbox"])


def test_database_url_from_settings(
    pg_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PRAXIS_DATABASE_URL", pg_url)
    assert run(capsys, "inbox") == (0, {})


def test_stripe_source_needs_a_test_key() -> None:
    with pytest.raises(SystemExit, match="STRIPE_SECRET_KEY"):
        stripe_source(Settings(), MemoryRefStore())
    gw = stripe_source(Settings(stripe_secret_key=SecretStr(sandbox_key())), MemoryRefStore())
    assert isinstance(gw, StripeGateway) and gw.provider == "stripe"
    assert datetime.now(UTC) > T0
