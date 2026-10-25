"""Ops CLI: ``validate`` (config + runbooks) and ``synthetic-alert`` (incident-flow drill)."""

from __future__ import annotations

import argparse
import json
import sys

from praxis.config import get_settings
from praxis.control.db import make_engine
from praxis.logging import configure_logging
from praxis.ops.config import load_ops_config
from praxis.ops.incidents import (
    IncidentManager,
    IncidentStore,
    LogNotifier,
    MemoryIncidentStore,
    PostgresIncidentStore,
)
from praxis.ops.monitor import synthetic_alert
from praxis.ops.runbooks import validate_runbooks


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="praxis.ops")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate").set_defaults(fn=cmd_validate)
    syn = sub.add_parser("synthetic-alert")
    syn.add_argument("--category", choices=["service", "data", "model", "business"], required=True)
    syn.add_argument("--persist", action="store_true", help="write to the control-plane database")
    syn.set_defaults(fn=cmd_synthetic_alert)
    args = ap.parse_args(argv)
    configure_logging("praxis-ops", get_settings().log_level.value)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
