"""CLI for the event backbone. All data is SYNTHETIC.

    python -m praxis.streaming migrate
    python -m praxis.streaming run-local --customers 1000 --days 28 --duplicate-rate 0.1 ...
    python -m praxis.streaming emulator-setup --project praxis-local
    python -m praxis.streaming produce --project praxis-local --customers 100 --days 14
    python -m praxis.streaming consume --project praxis-local --role operational
    python -m praxis.streaming emulator-bench --project praxis-local --customers 200 --days 14
    python -m praxis.streaming replay --archive data/events/archive --project praxis-local
    python -m praxis.streaming dlq [--redrive --project praxis-local]

The database URL comes from ``--database-url`` or ``PRAXIS_DATABASE_URL``; it is never
printed. Pub/Sub commands target the emulator (``PUBSUB_EMULATOR_HOST``); the cloud
topology is owned by Terraform.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from praxis import __version__
from praxis.config import get_settings
from praxis.control.db import make_engine, migrate
from praxis.control.store import ControlPlaneStore, snapshot_checksum
from praxis.data.warehouse import Warehouse
from praxis.logging import configure_logging
from praxis.simulator.config import load_config
from praxis.simulator.engine import Engine
from praxis.simulator.population import generate_population
from praxis.simulator.runner import peak_rss_mb
from praxis.streaming.archive import EventArchive
from praxis.streaming.consumers.dead_letter import DeadLetterWorker
from praxis.streaming.consumers.monitoring import MonitoringConsumer
from praxis.streaming.consumers.operational import OperationalConsumer
from praxis.streaming.consumers.warehouse import WarehouseConsumer
from praxis.streaming.faults import CrashPlan
from praxis.streaming.memory import FaultPlan
from praxis.streaming.metrics import StreamMetrics
from praxis.streaming.pipeline import (
    ROLES,
    LocalPipeline,
    expected_state,
    observed_state,
    subscription_consistent,
)
from praxis.streaming.producer import EventProducer, replay_archive
from praxis.streaming.pubsub import (
    EMULATOR_ENV,
    PubSubPublisher,
    PubSubPuller,
    ensure_topology,
    run_pull_loop,
)
from praxis.streaming.redrive import redrive_dead_letters
from praxis.streaming.runtime import Consumer, ConsumerWorker, DeadLetterRouter
from praxis.streaming.topology import (
    DLQ_INSPECT,
    MONITORING,
    OPERATIONAL,
    WAREHOUSE,
    Topology,
    build_topology,
)
from praxis.streaming.transport import Worker


def _db_url(args: argparse.Namespace) -> str:
    if args.database_url:
        return str(args.database_url)
    secret = get_settings().database_url
    if secret is None:
        raise SystemExit("set --database-url or PRAXIS_DATABASE_URL")
    return secret.get_secret_value()


def _topology(args: argparse.Namespace) -> Topology:
    settings = get_settings()
    return build_topology(
        settings.gcp_resource_prefix,
        args.environment or settings.environment.value,
        min_backoff_s=args.min_backoff,
        max_backoff_s=max(args.min_backoff, args.max_backoff),
        ack_deadline_s=args.ack_deadline,
    )


def _sim(customers: int, days: int, seed: int) -> tuple[Iterator[dict[str, Any]], int]:
    cfg = load_config().with_overrides(n_customers=customers, days=days)
    return Engine(cfg, seed, generate_population(cfg, seed)).run(), cfg.billing.max_attempts


def _write(report: dict[str, Any], out: Path | None) -> None:
    text = json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
    sys.stdout.write(text)


# --- commands -----------------------------------------------------------------------
def cmd_migrate(args: argparse.Namespace) -> int:
    migrate(_db_url(args))
    sys.stdout.write("control plane migrated to head\n")
    return 0


def cmd_run_local(args: argparse.Namespace) -> int:
    """Simulate -> publish (with faults) -> consume -> verify against the oracle."""
    engine = make_engine(_db_url(args))
    store = ControlPlaneStore(engine)
    events, max_attempts = _sim(args.customers, args.days, args.seed)
    events_list = list(events)
    crash = CrashPlan(seed=args.seed, before_rate=args.crash_rate, after_rate=args.crash_rate)
    with Warehouse(args.duckdb) as warehouse:
        warehouse.migrate()
        pipeline = LocalPipeline(
            store,
            warehouse,
            archive_root=args.archive,
            topology=build_topology(environment="local"),
            faults=FaultPlan(
                seed=args.seed,
                duplicate_rate=args.duplicate_rate,
                max_delay_s=args.max_delay,
                reorder=args.reorder,
            ),
            crash_plans={OPERATIONAL: crash, WAREHOUSE: crash} if args.crash_rate else {},
            batch_size=args.batch_size,
        )
        started = time.perf_counter()
        published = pipeline.run(events_list, chunk=args.chunk)
        elapsed = time.perf_counter() - started
        before_redrive = store.dead_letter_breakdown()
        # Chaos harness only: exercise the operator redrive so recovery is measured too.
        redriven = redrive_dead_letters(store, pipeline.broker, pipeline.topology.events_topic)
        pipeline.drain()
        snap = store.snapshot()
        expected = expected_state(events_list, max_attempts)
        report = {
            "is_synthetic": True,
            "praxis_version": __version__,
            "transport": "memory",
            "customers": args.customers,
            "days": args.days,
            "seed": args.seed,
            "faults": vars(pipeline.faults),
            "crash_rate": args.crash_rate,
            "events_published": published,
            "elapsed_s": round(elapsed, 3),
            "events_per_s": round(published / elapsed, 1),
            "peak_rss_mb": round(peak_rss_mb(), 1),
            "matches_oracle": observed_state(snap) == expected,
            "subscription_inconsistencies": len(subscription_consistent(snap)),
            "control_plane_checksum": snapshot_checksum(snap),
            "customers_projected": len(snap["customers"]),
            "invoices_projected": len(snap["invoices"]),
            "ledger_entries": snap["ledger_entries"],
            "ledger_total_minor": snap["ledger_total_minor"],
            "warehouse_rows": warehouse.count("raw.sim_events"),
            "pending": store.pending_summary(),
            "dead_letters_before_redrive": before_redrive,
            "redrive": vars(redriven),
            "dead_letters_after_redrive": store.dead_letter_counts(),
            "drain": {k: vars(v) for k, v in pipeline.reports.items()},
            "metrics": pipeline.metrics.snapshot(),
        }
    engine.dispose()
    _write(report, args.report)
    return 0 if report["matches_oracle"] else 1


def _require_emulator() -> None:
    if not os.environ.get(EMULATOR_ENV):
        raise SystemExit(f"{EMULATOR_ENV} must point at the Pub/Sub emulator")


def cmd_emulator_setup(args: argparse.Namespace) -> int:
    _require_emulator()
    created = ensure_topology(args.project, _topology(args))
    sys.stdout.write(json.dumps({"created": created}) + "\n")
    return 0


def cmd_produce(args: argparse.Namespace) -> int:
    _require_emulator()
    topo = _topology(args)
    archive = EventArchive(args.archive) if args.archive else None
    producer = EventProducer(PubSubPublisher(args.project), topo.events_topic, archive)
    events, _ = _sim(args.customers, args.days, args.seed)
    total, batch = 0, []
    for event in events:
        batch.append(event)
        if len(batch) >= 1000:
            total += producer.publish(batch).published
            batch = []
    if batch:
        total += producer.publish(batch).published
    sys.stdout.write(json.dumps({"published": total}) + "\n")
    return 0


def _worker(
    role: str,
    topo: Topology,
    store: ControlPlaneStore,
    warehouse: Warehouse | None,
    metrics: StreamMetrics,
    publisher: PubSubPublisher,
) -> Worker:
    if role == DLQ_INSPECT:
        return DeadLetterWorker(store, metrics)
    router = DeadLetterRouter(publisher, topo.dead_letter_topic)
    consumer: Consumer
    if role == OPERATIONAL:
        consumer = OperationalConsumer(store)
    elif role == WAREHOUSE:
        assert warehouse is not None  # noqa: S101 - guaranteed by caller
        consumer = WarehouseConsumer(warehouse, f"stream:{topo.by_role(role).name}")
    else:
        consumer = MonitoringConsumer(metrics)
    return ConsumerWorker(consumer, router, metrics)


def cmd_consume(args: argparse.Namespace) -> int:
    _require_emulator()
    topo = _topology(args)
    engine = make_engine(_db_url(args))
    store = ControlPlaneStore(engine)
    metrics = StreamMetrics()
    warehouse = Warehouse(args.duckdb) if args.role == WAREHOUSE else None
    if warehouse is not None:
        warehouse.migrate()
    worker = _worker(args.role, topo, store, warehouse, metrics, PubSubPublisher(args.project))
    puller = PubSubPuller(args.project, topo.by_role(args.role).name)
    report = run_pull_loop(puller, worker, idle_timeout_s=args.idle_timeout)
    puller.close()
    if warehouse is not None:
        warehouse.close()
    engine.dispose()
    _write({"role": args.role, "loop": vars(report), "metrics": metrics.snapshot()}, None)
    return 0


def cmd_emulator_bench(args: argparse.Namespace) -> int:
    """Live latency: producer publishes in real time while all consumers pull concurrently."""
    _require_emulator()
    topo = _topology(args)
    ensure_topology(args.project, topo)
    engine = make_engine(_db_url(args), pool_size=4)
    store = ControlPlaneStore(engine)
    metrics = StreamMetrics()
    publisher = PubSubPublisher(args.project)
    events, max_attempts = _sim(args.customers, args.days, args.seed)
    events_list = list(events)
    with Warehouse(args.duckdb) as warehouse:
        warehouse.migrate()
        loops: dict[str, Any] = {}

        def consume(role: str) -> None:
            worker = _worker(role, topo, store, warehouse, metrics, publisher)
            puller = PubSubPuller(args.project, topo.by_role(role).name)
            loops[role] = vars(run_pull_loop(puller, worker, idle_timeout_s=args.idle_timeout))
            puller.close()

        # DuckDB connections are not thread-safe: the warehouse consumer runs alone after.
        threads = [threading.Thread(target=consume, args=(r,)) for r in (OPERATIONAL, MONITORING)]
        for t in threads:
            t.start()
        producer = EventProducer(publisher, topo.events_topic)
        started = time.perf_counter()
        interval = 1.0 / args.rate if args.rate else 0.0
        for i in range(0, len(events_list), args.publish_batch):
            producer.publish(events_list[i : i + args.publish_batch])
            if interval:
                # Pace the producer to a steady offered load (a load profile, not a sync hack).
                time.sleep(
                    max(0.0, started + (i + args.publish_batch) * interval - time.perf_counter())
                )
        publish_s = time.perf_counter() - started
        for t in threads:
            t.join()
        consume(WAREHOUSE)
        consume(DLQ_INSPECT)
        snap = store.snapshot()
        report = {
            "is_synthetic": True,
            "transport": "pubsub-emulator",
            "customers": args.customers,
            "days": args.days,
            "events": len(events_list),
            "offered_rate_eps": args.rate or "unthrottled",
            "publish_s": round(publish_s, 3),
            "matches_oracle": observed_state(snap) == expected_state(events_list, max_attempts),
            "warehouse_rows": warehouse.count("raw.sim_events"),
            "dead_letters": store.dead_letter_counts(),
            "loops": loops,
            "metrics": metrics.snapshot(),
            "note": "warehouse consumer drained after publishing (DuckDB single writer); its "
            "end_to_end latency therefore includes backlog and is not a live-latency figure",
        }
    engine.dispose()
    _write(report, args.report)
    return 0 if report["matches_oracle"] else 1


def cmd_replay(args: argparse.Namespace) -> int:
    _require_emulator()
    topo = _topology(args)
    producer = EventProducer(PubSubPublisher(args.project), topo.events_topic)
    n = replay_archive(EventArchive(args.archive), producer)
    sys.stdout.write(json.dumps({"replayed": n}) + "\n")
    return 0


def cmd_dlq(args: argparse.Namespace) -> int:
    engine = make_engine(_db_url(args))
    store = ControlPlaneStore(engine)
    out: dict[str, Any] = {"open_dead_letters": store.dead_letter_counts()}
    if args.redrive:
        _require_emulator()
        topo = _topology(args)
        report = redrive_dead_letters(
            store, PubSubPublisher(args.project), topo.events_topic, reason=args.reason
        )
        out["redrive"] = vars(report)
    engine.dispose()
    _write(out, None)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="praxis.streaming", description=__doc__.splitlines()[0])
    ap.add_argument("--database-url", default=None, help="overrides PRAXIS_DATABASE_URL")
    ap.add_argument("--environment", default=None, help="topology environment name")
    ap.add_argument("--min-backoff", type=float, default=10.0)
    ap.add_argument("--max-backoff", type=float, default=300.0)
    ap.add_argument("--ack-deadline", type=int, default=30)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)

    local = sub.add_parser("run-local")
    local.add_argument("--customers", type=int, default=1000)
    local.add_argument("--days", type=int, default=28)
    local.add_argument("--seed", type=int, default=42)
    local.add_argument("--duplicate-rate", type=float, default=0.1)
    local.add_argument("--max-delay", type=float, default=600.0)
    local.add_argument("--reorder", action=argparse.BooleanOptionalAction, default=True)
    local.add_argument("--crash-rate", type=float, default=0.005)
    local.add_argument("--chunk", type=int, default=5000)
    local.add_argument("--batch-size", type=int, default=100)
    local.add_argument("--duckdb", type=Path, default=None, help="default: in-memory")
    local.add_argument("--archive", type=Path, default=None)
    local.add_argument("--report", type=Path, default=None)
    local.set_defaults(fn=cmd_run_local)

    setup = sub.add_parser("emulator-setup")
    setup.add_argument("--project", default="praxis-local")
    setup.set_defaults(fn=cmd_emulator_setup)

    produce = sub.add_parser("produce")
    produce.add_argument("--project", default="praxis-local")
    produce.add_argument("--customers", type=int, default=100)
    produce.add_argument("--days", type=int, default=14)
    produce.add_argument("--seed", type=int, default=42)
    produce.add_argument("--archive", type=Path, default=None)
    produce.set_defaults(fn=cmd_produce)

    consume = sub.add_parser("consume")
    consume.add_argument("--project", default="praxis-local")
    consume.add_argument("--role", choices=[*ROLES, DLQ_INSPECT], required=True)
    consume.add_argument("--idle-timeout", type=float, default=10.0)
    consume.add_argument("--duckdb", type=Path, default=Path("data/warehouse/stream.duckdb"))
    consume.set_defaults(fn=cmd_consume)

    bench = sub.add_parser("emulator-bench")
    bench.add_argument("--project", default="praxis-local")
    bench.add_argument("--customers", type=int, default=200)
    bench.add_argument("--days", type=int, default=14)
    bench.add_argument("--seed", type=int, default=42)
    bench.add_argument("--rate", type=float, default=2000.0, help="offered events/s (0 = max)")
    bench.add_argument("--publish-batch", type=int, default=200)
    bench.add_argument("--idle-timeout", type=float, default=5.0)
    bench.add_argument("--duckdb", type=Path, default=None)
    bench.add_argument("--report", type=Path, default=None)
    bench.set_defaults(fn=cmd_emulator_bench)

    replay = sub.add_parser("replay")
    replay.add_argument("--project", default="praxis-local")
    replay.add_argument("--archive", type=Path, required=True)
    replay.set_defaults(fn=cmd_replay)

    dlq = sub.add_parser("dlq")
    dlq.add_argument("--redrive", action="store_true")
    dlq.add_argument("--reason", default=None)
    dlq.add_argument("--project", default="praxis-local")
    dlq.set_defaults(fn=cmd_dlq)

    args = ap.parse_args(argv)
    settings = get_settings()
    configure_logging("praxis-streaming", settings.log_level.value)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
