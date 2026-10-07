"""CLI: ``python -m praxis.payments [--database-url URL] {process|inbox|requeue}``.

* ``process``: drain the webhook inbox until nothing is due: re-fetch each
  changed Stripe object, publish the derived internal events to Pub/Sub (``--project``; the
  emulator when ``PUBSUB_EMULATOR_HOST`` is set) and mark the rows. Exit 1 if any row failed.
* ``inbox``: row counts by status (JSON).
* ``requeue``: make ``failed`` rows due again (after a fix).

External boundaries (Stripe client, Pub/Sub publisher) are built by injectable factories.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime

from praxis.config import Settings, get_settings
from praxis.control.db import make_engine
from praxis.logging import configure_logging
from praxis.payments.gateway import SnapshotSource
from praxis.payments.processor import EventPublisher, NotificationProcessor, ProcessReport
from praxis.payments.store import PostgresInbox, PostgresRefStore, RefStore
from praxis.payments.stripe_client import StripeClient
from praxis.payments.stripe_gateway import StripeGateway

GatewayFactory = Callable[[Settings, RefStore], SnapshotSource]
PublisherFactory = Callable[[argparse.Namespace], EventPublisher]


def stripe_source(settings: Settings, refs: RefStore) -> SnapshotSource:
    if settings.stripe_secret_key is None:
        raise SystemExit("PRAXIS_STRIPE_SECRET_KEY is required to process Stripe webhooks")
    client = StripeClient(
        settings.stripe_secret_key,
        api_version=settings.stripe_api_version,
        base_url=settings.stripe_api_base_url,
        timeout_s=settings.stripe_timeout_s,
    )
    return StripeGateway(client, refs)


def pubsub_publisher(args: argparse.Namespace) -> EventPublisher:  # pragma: no cover - cloud I/O
    from praxis.streaming.producer import EventProducer
    from praxis.streaming.pubsub import PubSubPublisher
    from praxis.streaming.topology import build_topology

    topology = build_topology(environment=args.environment)
    return EventProducer(PubSubPublisher(args.project), topology.events_topic)


def _url(args: argparse.Namespace, settings: Settings) -> str:
    url = args.database_url or (
        settings.database_url.get_secret_value() if settings.database_url else None
    )
    if not url:
        raise SystemExit("a database URL is required (--database-url or PRAXIS_DATABASE_URL)")
    return url


def _print(payload: object) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def main(
    argv: list[str] | None = None,
    *,
    gateway_factory: GatewayFactory = stripe_source,
    publisher_factory: PublisherFactory = pubsub_publisher,
) -> int:
    ap = argparse.ArgumentParser(prog="praxis.payments", description=__doc__.splitlines()[0])
    ap.add_argument("--database-url", default=None, help="overrides PRAXIS_DATABASE_URL")
    sub = ap.add_subparsers(dest="cmd", required=True)
    process = sub.add_parser("process")
    process.add_argument("--project", default="praxis-local", help="Pub/Sub project id")
    process.add_argument("--environment", default="local", help="topology environment name")
    process.add_argument("--limit", type=int, default=100)
    process.add_argument("--max-rounds", type=int, default=1000)
    sub.add_parser("inbox")
    sub.add_parser("requeue")
    args = ap.parse_args(argv)

    settings = get_settings()
    configure_logging("praxis-payments", settings.log_level.value)
    engine = make_engine(_url(args, settings))
    inbox = PostgresInbox(engine)
    try:
        if args.cmd == "inbox":
            _print(inbox.counts())
            return 0
        if args.cmd == "requeue":
            _print({"requeued": inbox.requeue_failed(datetime.now(UTC))})
            return 0
        source = gateway_factory(settings, PostgresRefStore(engine))
        processor = NotificationProcessor(inbox, {source.provider: source}, publisher_factory(args))
        report: ProcessReport = processor.drain(limit=args.limit, max_rounds=args.max_rounds)
        _print(asdict(report))
        return 1 if report.failed else 0
    finally:
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
