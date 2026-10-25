"""Ops CLI: ``validate`` (config + runbooks), ``synthetic-alert`` (incident-flow drill) and
``run`` (the ops monitor: the scheduler for ``OpsMonitor.tick``, ADR 0020)."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from praxis.config import get_settings
from praxis.control.db import make_engine
from praxis.control.metric_snapshots import MetricSnapshotStore, SnapshotPublisher
from praxis.errors import TransientError
from praxis.logging import configure_logging
from praxis.ops.config import load_ops_config
from praxis.ops.incidents import (
    IncidentManager,
    IncidentStore,
    LogNotifier,
    MemoryIncidentStore,
    PostgresIncidentStore,
)
from praxis.ops.monitor import OpsMonitor, Tick, synthetic_alert
from praxis.ops.runbooks import validate_runbooks
from praxis.ops.system import build_reader

logger = logging.getLogger(__name__)
COMPONENT = "ops-monitor"
PRUNE_AFTER = timedelta(days=1)  # expired rows of restarted processes are kept a day


def cmd_validate(_args: argparse.Namespace) -> int:
    cfg = load_ops_config()
    problems = validate_runbooks(cfg)
    print(  # noqa: T201 - CLI output
        json.dumps({"slos": len(cfg.slos), "rules": len(cfg.rules), "problems": problems}, indent=2)
    )
    return 1 if problems else 0


def cmd_synthetic_alert(args: argparse.Namespace) -> int:
    cfg = load_ops_config()
    settings = get_settings()
    store: IncidentStore = MemoryIncidentStore()
    if args.persist:
        if settings.database_url is None:
            print("PRAXIS_DATABASE_URL is required with --persist", file=sys.stderr)  # noqa: T201
            return 2
        store = PostgresIncidentStore(make_engine(settings.database_url.get_secret_value()))
    manager = IncidentManager(store, LogNotifier())
    report = manager.process([synthetic_alert(cfg, args.category)])
    print(json.dumps([i.as_dict() for i in report.opened], indent=2))  # noqa: T201
    return 0


def _summary(tick: Tick) -> dict[str, Any]:
    h = tick.health
    return {
        "at": h.generated_at.isoformat(),
        "status": h.status,
        "alerts": sorted(a.rule_id for a in h.alerts()),
        "opened": [i.incident_id for i in tick.incidents.opened],
        "resolved": [i.incident_id for i in tick.incidents.resolved],
        "stale_processes": h.inputs.get("stale_processes", []),
        "collectors": {k: v.get("status") for k, v in h.inputs.get("collectors", {}).items()},
    }


def _tick_once(
    monitor: OpsMonitor, publisher: SnapshotPublisher, snapshots: MetricSnapshotStore
) -> dict[str, Any]:
    """One tick; a database outage is reported and the loop carries on (next tick)."""
    now = datetime.now(UTC)
    try:
        tick = monitor.tick(now)
        snapshots.prune(now - PRUNE_AFTER)
    except TransientError as error:
        logger.error("ops tick failed", extra={"error": str(error)})
        return {"at": now.isoformat(), "status": "tick_failed", "error": str(error)}
    publisher.publish(now)
    return _summary(tick)


def cmd_run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if settings.database_url is None:
        print("PRAXIS_DATABASE_URL is required for the ops monitor", file=sys.stderr)  # noqa: T201
        return 2
    engine = make_engine(settings.database_url.get_secret_value())
    snapshots = MetricSnapshotStore(engine)
    interval = timedelta(seconds=args.interval)
    publisher = SnapshotPublisher(snapshots, COMPONENT, interval=interval,
                                  ttl=max(interval * 4, timedelta(minutes=1)))  # fmt: skip
    reader = build_reader(engine, args.warehouse or settings.warehouse_path,
                          own=(COMPONENT, publisher.instance))  # fmt: skip
    manager = IncidentManager(PostgresIncidentStore(engine), LogNotifier())
    monitor = OpsMonitor(load_ops_config(), manager, reader)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    ticks, failed = 0, 0
    while not stop.is_set():
        summary = _tick_once(monitor, publisher, snapshots)
        failed += summary["status"] == "tick_failed"
        print(json.dumps(summary), flush=True)  # noqa: T201 - one JSON line per tick
        ticks += 1
        if (args.max_ticks and ticks >= args.max_ticks) or stop.wait(args.interval):
            break
    engine.dispose()
    return 1 if failed == ticks else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="praxis.ops")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate").set_defaults(fn=cmd_validate)
    syn = sub.add_parser("synthetic-alert")
    syn.add_argument("--category", choices=["service", "data", "model", "business"], required=True)
    syn.add_argument("--persist", action="store_true", help="write to the control-plane database")
    syn.set_defaults(fn=cmd_synthetic_alert)
    run = sub.add_parser("run", help="evaluate alerts into incidents every --interval seconds")
    run.add_argument("--interval", type=float, default=60.0)
    run.add_argument("--max-ticks", type=int, default=0, help="stop after N ticks (0: forever)")
    run.add_argument("--warehouse", type=Path, default=None, help="DuckDB file (read-only)")
    run.set_defaults(fn=cmd_run)
    args = ap.parse_args(argv)
    configure_logging("praxis-ops", get_settings().log_level.value)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
